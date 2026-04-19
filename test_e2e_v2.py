"""端到端验证: 4项任务全覆盖"""
import requests
import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

BASE = "http://127.0.0.1:8000/api"
passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] %s" % name)
    else:
        failed += 1
        print("  [FAIL] %s -- %s" % (name, detail))


# === Part 1: 静态验证 ===
print("=" * 70)
print("Part 1: 模型字段验证 (Task 1)")
print("=" * 70)

from src.models.models import ChargeOrder, ChargingPile, PileQueue, OrderStatus

# 1.1 ChargeOrder 字段
co_cols = {c.name for c in ChargeOrder.__table__.columns}
for field in ["bill_code", "charge_start_time", "charge_end_time", "charge_duration",
              "total_power", "power_fee", "service_fee", "total_fee"]:
    check("ChargeOrder has %s" % field, field in co_cols, str(co_cols))

for old_field in ["charge_kwh", "electricity_fee", "end_soc", "timeout_fee"]:
    check("ChargeOrder no %s" % old_field, old_field not in co_cols, str(co_cols))

# 1.2 ChargingPile 字段
cp_cols = {c.name for c in ChargingPile.__table__.columns}
for field in ["total_charge_count", "total_charge_duration", "total_charge_amount"]:
    check("ChargingPile has %s" % field, field in cp_cols, str(cp_cols))

# 1.3 PileQueue 字段
pq_cols = {c.name for c in PileQueue.__table__.columns}
for field in ["pile_id", "order_id", "position", "entered_at"]:
    check("PileQueue has %s" % field, field in pq_cols, str(pq_cols))

# 1.4 OrderStatus
check("OrderStatus.PENDING", OrderStatus.PENDING == "PENDING")
check("OrderStatus.QUEUING", OrderStatus.QUEUING == "QUEUING")
check("OrderStatus.CHARGING", OrderStatus.CHARGING == "CHARGING")
check("OrderStatus.COMPLETED", OrderStatus.COMPLETED == "COMPLETED")
check("OrderStatus.CANCELLED", OrderStatus.CANCELLED == "CANCELLED")
check("OrderStatus.FAULTED", OrderStatus.FAULTED == "FAULTED")

# === Part 2: 计费引擎验证 (Task 2) ===
print()
print("=" * 70)
print("Part 2: 计费引擎验证 (Task 2)")
print("=" * 70)

from src.core.billing import calculate_fee, get_rate_for_hour, PEAK_RATE, FLAT_RATE, VALLEY_RATE
from datetime import datetime
import inspect

# 签名: 3 参数
sig = inspect.signature(calculate_fee)
params = list(sig.parameters.keys())
check("calculate_fee(start_time, end_time, total_kwh)",
      params == ["start_time", "end_time", "total_kwh"],
      "actual: %s" % params)

# 模块级函数（不是类方法）
check("calculate_fee 是模块级函数", callable(calculate_fee) and not hasattr(calculate_fee, '__self__'))

# 费率验证
for h in [10, 11, 12, 13, 14, 18, 19, 20]:
    check("hour %02d = PEAK(1.0)" % h, get_rate_for_hour(h) == PEAK_RATE)
for h in [7, 8, 9, 15, 16, 17, 21, 22]:
    check("hour %02d = FLAT(0.7)" % h, get_rate_for_hour(h) == FLAT_RATE)
for h in [23, 0, 1, 2, 3, 4, 5, 6]:
    check("hour %02d = VALLEY(0.4)" % h, get_rate_for_hour(h) == VALLEY_RATE)

# 计费计算: 纯波峰 11:00-13:00, 30度
bill = calculate_fee(datetime(2026, 1, 1, 11, 0), datetime(2026, 1, 1, 13, 0), 30.0)
check("纯波峰 power_fee=30.0", abs(bill["power_fee"] - 30.0) < 0.01,
      "actual=%.2f" % bill["power_fee"])
check("纯波峰 service_fee=24.0", abs(bill["service_fee"] - 24.0) < 0.01)
check("纯波峰 total_fee=54.0", abs(bill["total_fee"] - 54.0) < 0.01)
check("返回 duration_hours", "duration_hours" in bill)
check("duration_hours=2.0", abs(bill["duration_hours"] - 2.0) < 0.01)
check("detail 有 peak_minutes", "peak_minutes" in bill["detail"])
check("peak_minutes=120", bill["detail"]["peak_minutes"] == 120,
      "actual=%s" % bill["detail"]["peak_minutes"])

# 纯波谷 01:00-04:00, 20度
bill = calculate_fee(datetime(2026, 1, 1, 1, 0), datetime(2026, 1, 1, 4, 0), 20.0)
check("纯波谷 power_fee=8.0", abs(bill["power_fee"] - 8.0) < 0.01,
      "actual=%.2f" % bill["power_fee"])

# 跨时段 06:00-08:00, 12度
bill = calculate_fee(datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 1, 8, 0), 12.0)
expected = round(6.0 * 0.4 + 6.0 * 0.7, 2)  # 6.6
check("跨时段 power_fee=6.6", abs(bill["power_fee"] - expected) < 0.01,
      "actual=%.2f" % bill["power_fee"])

# 0度数
bill = calculate_fee(datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 14, 0), 0.0)
check("0度数 total_fee=0", abs(bill["total_fee"]) < 0.001)


# === Part 3: API集成验证 (Task 3) ===
print()
print("=" * 70)
print("Part 3: API 集成验证 (Task 3 + 自动调度阻断)")
print("=" * 70)

# 提交快充
r = requests.post(BASE + "/requests/", json={
    "vehicle_id": "V001", "charge_type": "Fast",
    "current_soc": 0.95, "target_soc": 1.0})
d = r.json()
check("快充提交成功", d["status"] == "success" and d.get("assigned_pile") is not None, str(d))

# 提交慢充
r = requests.post(BASE + "/requests/", json={
    "vehicle_id": "V002", "charge_type": "Slow",
    "current_soc": 0.3, "target_soc": 0.9})
check("慢充提交成功", r.json()["status"] == "success")

# 填满快充桩，制造排队
r2 = requests.post(BASE + "/requests/", json={
    "vehicle_id": "V003", "charge_type": "Fast",
    "current_soc": 0.95, "target_soc": 1.0})
r3 = requests.post(BASE + "/requests/", json={
    "vehicle_id": "V004", "charge_type": "Fast",
    "current_soc": 0.95, "target_soc": 1.0})

# 第4个快充 -> 排队
r4 = requests.post(BASE + "/requests/", json={
    "vehicle_id": "V005_QUEUED", "charge_type": "Fast",
    "current_soc": 0.1, "target_soc": 0.5})
d4 = r4.json()
check("第4个快充进入排队", d4.get("queue_position") is not None, str(d4))

# 等待快充桩充满
print("\n  等待快充桩充满... (~8秒)")
time.sleep(8)

# 验证自动调度阻断
status = requests.get(BASE + "/system/dump").json()
fast_piles = [p for p in status["piles"] if p["type"] == "Fast"]
idle_fast = [p for p in fast_piles if p["status"] == "IDLE"]
check("快充桩已释放为IDLE", len(idle_fast) == 3,
      "idle=%d, piles=%s" % (len(idle_fast), [(p["pile_id"], p["status"]) for p in fast_piles]))

charging_vehicles = [p["vehicle_id"] for p in fast_piles if p["status"] == "CHARGING"]
check("排队车辆未被自动调入", "V005_QUEUED" not in charging_vehicles,
      "charging: %s" % charging_vehicles)
check("快充队列仍有排队", status["fast_queue_count"] == 1,
      "queue=%d" % status["fast_queue_count"])

# 验证已完成订单账单
print()
print("--- 账单字段验证 ---")
r = requests.get(BASE + "/bills/1")
d = r.json()
check("订单1 COMPLETED", d["status"] == "COMPLETED", d.get("status"))
check("有 bill_code", d.get("bill_code") is not None and d["bill_code"].startswith("BILL"),
      str(d.get("bill_code")))
check("有 charge_start_time", d.get("charge_start_time") is not None)
check("有 charge_end_time", d.get("charge_end_time") is not None)
check("有 charge_duration", d.get("charge_duration") is not None)
check("有 total_power > 0", (d.get("total_power") or 0) > 0, str(d.get("total_power")))
check("有 power_fee > 0", (d.get("power_fee") or 0) > 0)
check("有 service_fee > 0", (d.get("service_fee") or 0) > 0)
check("有 total_fee > 0", (d.get("total_fee") or 0) > 0)
check("total_fee = power + service",
      abs((d.get("total_fee") or 0) - ((d.get("power_fee") or 0) + (d.get("service_fee") or 0))) < 0.02)
check("detail 有 peak_minutes",
      d.get("detail") is not None and "peak_minutes" in (d.get("detail") or {}))

# 充电桩统计验证
check("桩有 total_charge_count",
      any(p["total_charge_count"] > 0 for p in status["piles"]))

# 无旧字段
for old in ["end_soc", "charge_kwh", "electricity_fee", "timeout_fee", "left_at"]:
    check("账单无旧字段 %s" % old, old not in d, str(d.keys()))

# 取消 + 停止
r = requests.post(BASE + "/requests/5/cancel")
check("取消排队成功", r.status_code == 200, str(r.status_code))

# 停止慢充
r = requests.post(BASE + "/requests/2/stop")
d = r.json()
check("停止慢充成功", r.status_code == 200 and d["status"] == "success", str(d))
check("停止返回 total_power", "total_power" in d)
check("停止返回 power_fee", "power_fee" in d)

# === Part 4: 配置文件验证 (Task 4) ===
print()
print("=" * 70)
print("Part 4: 配置文件验证 (Task 4)")
print("=" * 70)
import yaml
with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

check("system.fast_pile_count=3", cfg["system"]["fast_pile_count"] == 3)
check("system.slow_pile_count=2", cfg["system"]["slow_pile_count"] == 2)
check("system.waiting_area_size=10", cfg["system"]["waiting_area_size"] == 10)
check("system.pile_queue_length=2", cfg["system"]["pile_queue_length"] == 2)
check("charging.fast_power=30.0", cfg["charging"]["fast_power"] == 30.0)
check("charging.slow_power=10.0", cfg["charging"]["slow_power"] == 10.0)
check("billing.peak_rate=1.0", cfg["billing"]["peak_rate"] == 1.0)
check("billing.flat_rate=0.7", cfg["billing"]["flat_rate"] == 0.7)
check("billing.valley_rate=0.4", cfg["billing"]["valley_rate"] == 0.4)
check("billing.service_fee_rate=0.8", cfg["billing"]["service_fee_rate"] == 0.8)
check("billing.peak_hours", cfg["billing"]["peak_hours"] == [[10, 15], [18, 21]])
check("billing.flat_hours", cfg["billing"]["flat_hours"] == [[7, 10], [15, 18], [21, 23]])
check("billing.valley_hours", cfg["billing"]["valley_hours"] == [[23, 24], [0, 7]])

# ===
print()
print("=" * 70)
print("FINAL: %d passed, %d failed" % (passed, failed))
if failed > 0:
    print("!!! SOME TESTS FAILED !!!")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
