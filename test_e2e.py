"""端到端测试 — 验证新计费规格（甲方新版字段）"""
import requests
import time

BASE = "http://127.0.0.1:8000/api"
passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} — {detail}")


print("=" * 60)
print("1. 提交充电请求（快充）")
r = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京A00001",
    "charge_type": "Fast",
    "current_soc": 0.2,
    "target_soc": 0.8
})
d = r.json()
check("提交成功", r.status_code == 200 and d["status"] == "success", str(d))
order1_pile = d.get("assigned_pile")
check("分配到桩", order1_pile is not None, str(d))

print()
print("2. 提交充电请求（慢充）")
r = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京A00002",
    "charge_type": "Slow",
    "current_soc": 0.3,
    "target_soc": 0.9
})
d = r.json()
check("提交成功", r.status_code == 200 and d["status"] == "success", str(d))

print()
print("3. 提交排队请求（快充队列）")
# 先把快充桩占满
r2 = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京A00003", "charge_type": "Fast",
    "current_soc": 0.1, "target_soc": 0.5
})
r3 = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京A00004", "charge_type": "Fast",
    "current_soc": 0.1, "target_soc": 0.5
})
# 这个应该排队
r4 = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京A00005", "charge_type": "Fast",
    "current_soc": 0.1, "target_soc": 0.5
})
d4 = r4.json()
check("进入排队", d4.get("queue_position") is not None, str(d4))
queue_order_id = 5  # 第5个订单

print()
print("4. 取消排队订单")
r = requests.post(f"{BASE}/requests/{queue_order_id}/cancel")
d = r.json()
check("取消成功", r.status_code == 200 and d["status"] == "success", str(d))

print()
print("5. 重复取消（应失败）")
r = requests.post(f"{BASE}/requests/{queue_order_id}/cancel")
check("重复取消返回400", r.status_code == 400, str(r.json()))

print()
print("6. 查询排队中订单的账单（无费用）")
r = requests.get(f"{BASE}/bills/2")
d = r.json()
check("账单查询成功", r.status_code == 200, str(d))
check("排队中无total_power", d.get("total_power") is None, str(d))

print()
print("7. 系统状态查询")
r = requests.get(f"{BASE}/system/dump")
d = r.json()
check("状态查询成功", r.status_code == 200, str(d))
check("包含piles列表", len(d.get("piles", [])) > 0, str(d))

print()
print("8. 等待充电进行 + 主动停止充电")
time.sleep(3)  # 让电量增长一些
r = requests.post(f"{BASE}/requests/1/stop")
d = r.json()
check("停止成功", r.status_code == 200 and d["status"] == "success", str(d))
check("返回total_power字段", "total_power" in d, str(d))
check("返回power_fee字段", "power_fee" in d, str(d))
check("返回service_fee字段", "service_fee" in d, str(d))
check("返回total_fee字段", "total_fee" in d, str(d))
check("total_power > 0", d.get("total_power", 0) > 0, str(d))

print()
print("9. 查询已停止订单的完整账单")
r = requests.get(f"{BASE}/bills/1")
d = r.json()
check("账单查询成功", r.status_code == 200, str(d))
check("状态为INTERRUPTED", d["status"] == "INTERRUPTED", d.get("status", ""))
check("total_power > 0", (d.get("total_power") or 0) > 0, str(d.get("total_power")))
check("power_fee > 0", (d.get("power_fee") or 0) > 0, str(d.get("power_fee")))
check("service_fee > 0", (d.get("service_fee") or 0) > 0, str(d.get("service_fee")))
check("total_fee = power + service",
      abs((d.get("total_fee") or 0) - ((d.get("power_fee") or 0) + (d.get("service_fee") or 0))) < 0.02,
      "total=%.2f, power=%.2f, svc=%.2f" % (d.get("total_fee", 0), d.get("power_fee", 0), d.get("service_fee", 0)))
check("detail包含peak/flat/valley",
      d.get("detail") is not None and "peak_kwh" in d.get("detail", {}),
      str(d.get("detail")))

print()
print("10. 不存在的订单")
r = requests.get(f"{BASE}/bills/999")
check("返回404", r.status_code == 404, str(r.status_code))

print()
print("11. 对排队中的订单尝试停止（应失败）")
# 先填满剩余慢充桩（已有1个慢充在充），再提交一个使其排队
for i in range(2):
    requests.post(f"{BASE}/requests/", json={
        "vehicle_id": "京B0000%d" % (i+1), "charge_type": "Slow",
        "current_soc": 0.1, "target_soc": 0.9
    })
r_q = requests.post(f"{BASE}/requests/", json={
    "vehicle_id": "京B00099", "charge_type": "Slow",
    "current_soc": 0.1, "target_soc": 0.9
})
dq = r_q.json()
queued_oid = 5 + 2 + 1  # order id = 8
if dq.get("queue_position") is not None:
    r_stop = requests.post(f"{BASE}/requests/%d/stop" % queued_oid)
    check("返回400", r_stop.status_code == 400, "code=%d" % r_stop.status_code)
else:
    check("返回400", False, "未能排队: %s" % str(dq))

print()
print("12. 无旧字段验证 — 账单不应包含旧字段")
r = requests.get(f"{BASE}/bills/1")
d = r.json()
check("无end_soc字段", "end_soc" not in d, str(d.keys()))
check("无charge_kwh字段", "charge_kwh" not in d, str(d.keys()))
check("无electricity_fee字段", "electricity_fee" not in d, str(d.keys()))
check("无timeout_fee字段", "timeout_fee" not in d, str(d.keys()))

print()
print("=" * 60)
print("Result: %d passed, %d failed" % (passed, failed))
if failed > 0:
    print("!!! SOME TESTS FAILED !!!")
else:
    print("ALL TESTS PASSED")
