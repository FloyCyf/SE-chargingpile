from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
#  可动态修改的计费配置（由 init_billing_config / update_billing_config 设置）
# ---------------------------------------------------------------------------

_billing_config = {
    "peak_rate": 1.0,
    "flat_rate": 0.7,
    "valley_rate": 0.4,
    "service_fee_rate": 0.8,
    "peak_hours": [[10, 15], [18, 21]],
    "flat_hours": [[7, 10], [15, 18], [21, 23]],
    "valley_hours": [[23, 24], [0, 7]],
}


def init_billing_config(config: dict):
    """从 config.yaml 初始化计费配置"""
    billing = config.get("billing", {})
    if billing:
        _billing_config["peak_rate"] = billing.get("peak_rate", 1.0)
        _billing_config["flat_rate"] = billing.get("flat_rate", 0.7)
        _billing_config["valley_rate"] = billing.get("valley_rate", 0.4)
        _billing_config["service_fee_rate"] = billing.get("service_fee_rate", 0.8)
        _billing_config["peak_hours"] = billing.get("peak_hours", [[10, 15], [18, 21]])
        _billing_config["flat_hours"] = billing.get("flat_hours", [[7, 10], [15, 18], [21, 23]])
        _billing_config["valley_hours"] = billing.get("valley_hours", [[23, 24], [0, 7]])


def get_billing_config() -> dict:
    """获取当前计费配置"""
    return dict(_billing_config)


def update_billing_config(new_config: dict):
    """动态更新计费配置"""
    for key in ("peak_rate", "flat_rate", "valley_rate", "service_fee_rate",
                "peak_hours", "flat_hours", "valley_hours"):
        if key in new_config:
            _billing_config[key] = new_config[key]


def _get_period_for_hour(hour: int) -> str:
    """根据小时判断所属时段：peak / flat / valley"""
    for start, end in _billing_config["peak_hours"]:
        if start <= hour < end:
            return "peak"
    for start, end in _billing_config["flat_hours"]:
        if start <= hour < end:
            return "flat"
    return "valley"


def calculate_fee(start_time: datetime, end_time: datetime,
                  total_kwh: float) -> dict:
    """
    按分钟切片计算费用，返回跨时段明细

    返回：
    {
        "total_power": float,
        "power_fee": float,
        "service_fee": float,
        "total_fee": float,
        "duration_hours": float,
        "detail": {
            "peak_minutes": int, "flat_minutes": int, "valley_minutes": int,
            "peak_kwh": float, "peak_fee": float,
            "flat_kwh": float, "flat_fee": float,
            "valley_kwh": float, "valley_fee": float,
            "segments": [
                {"period": "peak", "start": "10:00", "end": "15:00",
                 "minutes": 300, "kwh": 15.0, "rate": 1.0, "fee": 15.0},
                ...
            ]
        }
    }
    """
    result_zero = {
        "total_power": 0.0,
        "power_fee": 0.0,
        "service_fee": 0.0,
        "total_fee": 0.0,
        "duration_hours": 0.0,
        "detail": {
            "peak_minutes": 0, "flat_minutes": 0, "valley_minutes": 0,
            "peak_kwh": 0.0, "peak_fee": 0.0,
            "flat_kwh": 0.0, "flat_fee": 0.0,
            "valley_kwh": 0.0, "valley_fee": 0.0,
            "segments": [],
        },
    }

    if total_kwh <= 0 or start_time >= end_time:
        return result_zero

    peak_rate = _billing_config["peak_rate"]
    flat_rate = _billing_config["flat_rate"]
    valley_rate = _billing_config["valley_rate"]
    service_rate = _billing_config["service_fee_rate"]

    # 按分钟遍历，统计各时段分钟数并构建连续时段段
    peak_minutes = 0
    flat_minutes = 0
    valley_minutes = 0

    segments = []  # 连续时段段列表
    current = start_time
    one_minute = timedelta(minutes=1)
    prev_period = None
    seg_start = None

    while current < end_time:
        period = _get_period_for_hour(current.hour)
        if period == "peak":
            peak_minutes += 1
        elif period == "flat":
            flat_minutes += 1
        else:
            valley_minutes += 1

        # 构建连续时段段
        if period != prev_period:
            if prev_period is not None:
                segments.append({
                    "period": prev_period,
                    "start": seg_start.strftime("%H:%M"),
                    "end": current.strftime("%H:%M"),
                    "minutes": int((current - seg_start).total_seconds() / 60),
                })
            seg_start = current
            prev_period = period

        current += one_minute

    # 最后一个时段段
    if prev_period is not None and seg_start is not None:
        segments.append({
            "period": prev_period,
            "start": seg_start.strftime("%H:%M"),
            "end": current.strftime("%H:%M"),
            "minutes": int((current - seg_start).total_seconds() / 60),
        })

    total_minutes = peak_minutes + flat_minutes + valley_minutes
    if total_minutes <= 0:
        return result_zero

    # 按分钟占比分配度数到各时段
    peak_kwh = total_kwh * peak_minutes / total_minutes
    flat_kwh = total_kwh * flat_minutes / total_minutes
    valley_kwh = total_kwh * valley_minutes / total_minutes

    peak_fee = round(peak_kwh * peak_rate, 2)
    flat_fee = round(flat_kwh * flat_rate, 2)
    valley_fee = round(valley_kwh * valley_rate, 2)

    power_fee = round(peak_fee + flat_fee + valley_fee, 2)
    service_fee = round(total_kwh * service_rate, 2)
    total_fee = round(power_fee + service_fee, 2)
    duration_hours = round(
        (end_time - start_time).total_seconds() / 3600.0, 4)

    # 为每个 segment 分配电量和费用
    for seg in segments:
        rate_map = {"peak": peak_rate, "flat": flat_rate, "valley": valley_rate}
        seg_kwh = total_kwh * seg["minutes"] / total_minutes
        seg["kwh"] = round(seg_kwh, 4)
        seg["rate"] = rate_map.get(seg["period"], 0)
        seg["fee"] = round(seg_kwh * seg["rate"], 2)

    return {
        "total_power": round(total_kwh, 4),
        "power_fee": power_fee,
        "service_fee": service_fee,
        "total_fee": total_fee,
        "duration_hours": duration_hours,
        "detail": {
            "peak_minutes": peak_minutes,
            "flat_minutes": flat_minutes,
            "valley_minutes": valley_minutes,
            "peak_kwh": round(peak_kwh, 4),
            "peak_fee": peak_fee,
            "flat_kwh": round(flat_kwh, 4),
            "flat_fee": flat_fee,
            "valley_kwh": round(valley_kwh, 4),
            "valley_fee": valley_fee,
            "segments": segments,
        },
    }
