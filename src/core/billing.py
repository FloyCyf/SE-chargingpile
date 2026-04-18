from datetime import datetime, timedelta
from typing import Dict, Tuple


class BillingEngine:
    """
    分时阶梯计费引擎（甲方新规格）

    账单总额 = 分时电费 + 服务费

    - 分时电费：按充电时段落入的波峰/波平/波谷区间独立计价（分钟级切片）
    - 服务费：固定费率 0.80 元/度 × 总度数
    """

    def __init__(self, config: dict):
        billing_cfg = config.get('billing', {})

        self.service_fee_rate = billing_cfg.get('service_fee_rate', 0.80)
        self.battery_capacity_kwh = billing_cfg.get('battery_capacity_kwh', 60.0)

        # 当前待计费的充电度数，由调用方在调用 calculate_fee 前设置
        self._total_kwh: float = 0.0

        # 构建 24 小时费率查找表 hour -> (rate, tier_key)
        self.rate_table: Dict[int, Tuple[float, str]] = {}
        rates_cfg = billing_cfg.get('electricity_rates', {})
        self._build_rate_table(rates_cfg)

    def _build_rate_table(self, rates_cfg: dict):
        """根据配置构建 24 小时费率速查表"""
        for tier_key, tier_cfg in rates_cfg.items():
            rate = tier_cfg['rate']
            for period_str in tier_cfg.get('periods', []):
                start_str, end_str = period_str.split('-')
                start_h = int(start_str.split(':')[0])
                end_h = int(end_str.split(':')[0])

                if start_h < end_h:
                    for h in range(start_h, end_h):
                        self.rate_table[h] = (rate, tier_key)
                else:
                    # 跨日时段，如 23:00-07:00
                    for h in range(start_h, 24):
                        self.rate_table[h] = (rate, tier_key)
                    for h in range(0, end_h):
                        self.rate_table[h] = (rate, tier_key)

    def _get_rate_at(self, hour: int) -> Tuple[float, str]:
        """获取指定小时的电价费率和时段类型"""
        return self.rate_table.get(hour, (0.70, 'flat'))

    def soc_to_kwh(self, start_soc: float, end_soc: float) -> float:
        """将 SOC 差值转换为充电度数 (kWh)"""
        delta = max(0.0, end_soc - start_soc)
        return round(delta * self.battery_capacity_kwh, 4)

    def calculate_fee(self, start_time: datetime, end_time: datetime) -> Dict:
        """
        计算分时阶梯电费 + 服务费

        充电功率恒定，因此将充电度数按各时段占用时间的比例分配，
        再乘以各时段对应的费率独立求和。采用分钟级切片。

        调用前须通过 self._total_kwh 设置本次充电总度数。

        参数:
            start_time: 充电开始时间
            end_time:   充电结束时间

        返回: {
            "total_power": float,   # 总度数
            "power_fee":   float,   # 电费
            "service_fee": float,   # 服务费
            "total_fee":   float,   # 总费用
            "detail": {             # 分时明细
                "peak_kwh":   float,
                "flat_kwh":   float,
                "valley_kwh": float
            }
        }
        """
        total_kwh = self._total_kwh
        result_zero = {
            "total_power": 0.0,
            "power_fee": 0.0,
            "service_fee": 0.0,
            "total_fee": 0.0,
            "detail": {"peak_kwh": 0.0, "flat_kwh": 0.0, "valley_kwh": 0.0},
        }

        if total_kwh <= 0 or start_time >= end_time:
            return result_zero

        total_minutes = (end_time - start_time).total_seconds() / 60.0
        if total_minutes <= 0:
            return result_zero

        # 按小时边界切分充电区间，统计每个时段类型的累计分钟数
        tier_minutes: Dict[str, float] = {"peak": 0.0, "flat": 0.0, "valley": 0.0}
        tier_rates: Dict[str, float] = {"peak": 1.0, "flat": 0.7, "valley": 0.4}

        current = start_time
        while current < end_time:
            rate, tier_key = self._get_rate_at(current.hour)
            tier_rates[tier_key] = rate

            # 本小时段终点：取当前小时结束时刻与 end_time 的较小值
            next_hour = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            segment_end = min(next_hour, end_time)
            seg_minutes = (segment_end - current).total_seconds() / 60.0

            tier_minutes[tier_key] = tier_minutes.get(tier_key, 0.0) + seg_minutes
            current = segment_end

        # 按时间比例分配度数并计算费用
        peak_kwh = round(total_kwh * tier_minutes["peak"] / total_minutes, 4) if total_minutes > 0 else 0.0
        flat_kwh = round(total_kwh * tier_minutes["flat"] / total_minutes, 4) if total_minutes > 0 else 0.0
        valley_kwh = round(total_kwh * tier_minutes["valley"] / total_minutes, 4) if total_minutes > 0 else 0.0

        power_fee = round(
            peak_kwh * tier_rates["peak"]
            + flat_kwh * tier_rates["flat"]
            + valley_kwh * tier_rates["valley"],
            2,
        )

        service_fee = round(total_kwh * self.service_fee_rate, 2)
        total_fee = round(power_fee + service_fee, 2)

        return {
            "total_power": round(total_kwh, 4),
            "power_fee": power_fee,
            "service_fee": service_fee,
            "total_fee": total_fee,
            "detail": {
                "peak_kwh": peak_kwh,
                "flat_kwh": flat_kwh,
                "valley_kwh": valley_kwh,
            },
        }
