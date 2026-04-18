from datetime import datetime, timedelta
from typing import Dict, List, Tuple


class BillingEngine:
    """
    分时阶梯计费引擎
    
    账单总额 = 阶梯电费 + 服务费 + 超时占位费
    
    - 阶梯电费：按充电时段落入的波峰/波平/波谷区间独立计价
    - 服务费：固定费率 * 充电度数
    - 超时占位费：充满后豁免 N 分钟，逾期按分钟收费
    """

    def __init__(self, config: dict):
        billing_cfg = config.get('billing', {})

        self.service_fee_rate = billing_cfg.get('service_fee_rate', 0.80)
        self.timeout_fee_rate = billing_cfg.get('timeout_fee_rate', 1.00)
        self.free_parking_minutes = billing_cfg.get('free_parking_minutes', 30)
        self.battery_capacity_kwh = billing_cfg.get('battery_capacity_kwh', 60.0)

        # 构建 24 小时费率查找表 hour -> (rate, label)
        self.rate_table: Dict[int, Tuple[float, str]] = {}
        rates_cfg = billing_cfg.get('electricity_rates', {})
        self._build_rate_table(rates_cfg)

    def _build_rate_table(self, rates_cfg: dict):
        """根据配置构建 24 小时费率速查表"""
        for tier_key, tier_cfg in rates_cfg.items():
            rate = tier_cfg['rate']
            label = tier_cfg.get('label', tier_key)
            for period_str in tier_cfg.get('periods', []):
                start_str, end_str = period_str.split('-')
                start_h = int(start_str.split(':')[0])
                end_h = int(end_str.split(':')[0])

                if start_h < end_h:
                    for h in range(start_h, end_h):
                        self.rate_table[h] = (rate, label)
                else:
                    # 跨日时段，如 23:00-07:00
                    for h in range(start_h, 24):
                        self.rate_table[h] = (rate, label)
                    for h in range(0, end_h):
                        self.rate_table[h] = (rate, label)

    def _get_rate_at(self, hour: int) -> Tuple[float, str]:
        """获取指定小时的电价费率和时段名称"""
        return self.rate_table.get(hour, (0.70, '波平'))

    def soc_to_kwh(self, start_soc: float, end_soc: float) -> float:
        """将 SOC 差值转换为充电度数 (kWh)"""
        delta = max(0.0, end_soc - start_soc)
        return round(delta * self.battery_capacity_kwh, 4)

    def calculate_fee(self, start_time: datetime, end_time: datetime,
                      charge_kwh: float) -> Dict:
        """
        计算分时阶梯电费
        
        充电功率恒定，因此将充电度数按各时段占用时间的比例分配，
        再乘以各时段对应的费率独立求和。
        
        返回: {'electricity_fee': float, 'detail': [...]}
        """
        if charge_kwh <= 0 or start_time >= end_time:
            return {'electricity_fee': 0.0, 'detail': []}

        total_seconds = (end_time - start_time).total_seconds()
        if total_seconds <= 0:
            return {'electricity_fee': 0.0, 'detail': []}

        # 按小时边界切分充电区间，统计每个时段的累计秒数
        segments: Dict[str, Dict] = {}   # label -> {seconds, rate}
        current = start_time

        while current < end_time:
            rate, label = self._get_rate_at(current.hour)

            # 本小时段终点：取当前小时结束时刻与 end_time 的较小值
            next_hour = (current.replace(minute=0, second=0, microsecond=0)
                         + timedelta(hours=1))
            segment_end = min(next_hour, end_time)
            seg_seconds = (segment_end - current).total_seconds()

            if label not in segments:
                segments[label] = {'seconds': 0.0, 'rate': rate}
            segments[label]['seconds'] += seg_seconds

            current = segment_end

        # 按时间比例分配度数并计算费用
        total_fee = 0.0
        detail: List[Dict] = []
        for label, info in segments.items():
            proportion = info['seconds'] / total_seconds
            kwh_in_period = charge_kwh * proportion
            fee = round(kwh_in_period * info['rate'], 2)
            total_fee += fee
            detail.append({
                'period': label,
                'rate': info['rate'],
                'minutes': round(info['seconds'] / 60.0, 2),
                'kwh': round(kwh_in_period, 4),
                'fee': fee,
            })

        return {
            'electricity_fee': round(total_fee, 2),
            'detail': detail,
        }

    def calculate_service_fee(self, charge_kwh: float) -> float:
        """计算充电服务费 = 服务费率 * 充电度数"""
        if charge_kwh <= 0:
            return 0.0
        return round(charge_kwh * self.service_fee_rate, 2)

    def calculate_timeout_fee(self, finished_at: datetime,
                              left_at: datetime) -> float:
        """
        计算超时占位费
        
        充满后给予 free_parking_minutes 分钟豁免期，
        逾期后按 timeout_fee_rate 元/分钟 收取。
        """
        if finished_at is None or left_at is None:
            return 0.0
        if left_at <= finished_at:
            return 0.0

        parked_minutes = (left_at - finished_at).total_seconds() / 60.0
        overtime_minutes = max(0.0, parked_minutes - self.free_parking_minutes)

        if overtime_minutes <= 0:
            return 0.0
        return round(overtime_minutes * self.timeout_fee_rate, 2)

    def generate_bill(self, order_data: dict) -> Dict:
        """
        根据订单数据生成完整账单
        
        order_data 必须包含:
          - start_soc: float      初始电量
          - end_soc: float        结束电量
          - started_at: datetime  开始充电时间
          - finished_at: datetime 充电结束时间
        可选:
          - left_at: datetime     车辆离场时间（用于超时费计算）
        
        返回包含所有费用明细的字典
        """
        start_soc = order_data['start_soc']
        end_soc = order_data['end_soc']
        started_at = order_data['started_at']
        finished_at = order_data['finished_at']
        left_at = order_data.get('left_at')

        # 1. SOC 转换为充电度数
        charge_kwh = self.soc_to_kwh(start_soc, end_soc)

        # 2. 分时阶梯电费
        fee_result = self.calculate_fee(started_at, finished_at, charge_kwh)
        electricity_fee = fee_result['electricity_fee']

        # 3. 服务费
        service_fee = self.calculate_service_fee(charge_kwh)

        # 4. 超时占位费
        timeout_fee = 0.0
        if left_at is not None:
            timeout_fee = self.calculate_timeout_fee(finished_at, left_at)

        # 5. 最终总费用
        total_fee = round(electricity_fee + service_fee + timeout_fee, 2)

        return {
            'charge_kwh': charge_kwh,
            'electricity_fee': electricity_fee,
            'service_fee': service_fee,
            'timeout_fee': timeout_fee,
            'total_fee': total_fee,
            'fee_detail': fee_result['detail'],
        }
