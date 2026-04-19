from datetime import datetime, timedelta

PEAK_RATE = 1.0
FLAT_RATE = 0.7
VALLEY_RATE = 0.4
SERVICE_FEE_RATE = 0.8


def get_rate_for_hour(hour: int) -> float:
    """根据小时判断所属时段费率"""
    if (10 <= hour < 15) or (18 <= hour < 21):
        return PEAK_RATE
    elif (7 <= hour < 10) or (15 <= hour < 18) or (21 <= hour < 23):
        return FLAT_RATE
    else:
        return VALLEY_RATE


def calculate_fee(start_time: datetime, end_time: datetime, total_kwh: float) -> dict:
    """
    按分钟切片计算费用

    计算逻辑：
    1. 遍历每一分钟，判断属于峰/平/谷哪个时段
    2. 统计各时段分钟数
    3. 按分钟占比分配度数到各时段
    4. 电费 = 峰度数*1.0 + 平度数*0.7 + 谷度数*0.4
    5. 服务费 = 总度数 * 0.8
    6. 总费用 = 电费 + 服务费

    返回：
    {
        "total_power": float,
        "power_fee": float,
        "service_fee": float,
        "total_fee": float,
        "duration_hours": float,
        "detail": {"peak_minutes": int, "flat_minutes": int, "valley_minutes": int}
    }
    """
    result_zero = {
        "total_power": 0.0,
        "power_fee": 0.0,
        "service_fee": 0.0,
        "total_fee": 0.0,
        "duration_hours": 0.0,
        "detail": {"peak_minutes": 0, "flat_minutes": 0, "valley_minutes": 0},
    }

    if total_kwh <= 0 or start_time >= end_time:
        return result_zero

    # 按分钟遍历，统计各时段分钟数
    peak_minutes = 0
    flat_minutes = 0
    valley_minutes = 0

    current = start_time
    one_minute = timedelta(minutes=1)

    while current < end_time:
        rate = get_rate_for_hour(current.hour)
        if rate == PEAK_RATE:
            peak_minutes += 1
        elif rate == FLAT_RATE:
            flat_minutes += 1
        else:
            valley_minutes += 1
        current += one_minute

    total_minutes = peak_minutes + flat_minutes + valley_minutes
    if total_minutes <= 0:
        return result_zero

    # 按分钟占比分配度数到各时段
    peak_kwh = total_kwh * peak_minutes / total_minutes
    flat_kwh = total_kwh * flat_minutes / total_minutes
    valley_kwh = total_kwh * valley_minutes / total_minutes

    # 电费 = 各时段度数 * 各时段费率
    power_fee = round(
        peak_kwh * PEAK_RATE + flat_kwh * FLAT_RATE + valley_kwh * VALLEY_RATE, 2
    )

    # 服务费 = 总度数 * 0.8
    service_fee = round(total_kwh * SERVICE_FEE_RATE, 2)

    # 总费用 = 电费 + 服务费
    total_fee = round(power_fee + service_fee, 2)

    # 充电时长（小时）
    duration_hours = round((end_time - start_time).total_seconds() / 3600.0, 4)

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
        },
    }
