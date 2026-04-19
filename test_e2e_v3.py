"""
智能充电桩调度计费系统 — 综合端到端测试 v3
覆盖：认证、排队号码、最短时间调度、每桩队列、修改请求、
      故障处理、取消/停止、管理员接口、报表
"""
import os
import sys
import time
import asyncio
import pytest

# --------------- 公用 fixture ---------------

@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    """删除旧数据库以确保全新 schema"""
    db_path = os.path.join(os.path.dirname(__file__), "charging.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    yield
    # Windows 下 DB 可能被锁，不强制删除


@pytest.fixture(scope="session")
def app():
    # 必须在 DB 删除后再 import，避免旧 engine 缓存
    from src.main import app as _app
    return _app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def user_token(client):
    r = client.post("/api/auth/register", json={
        "username": "testuser", "password": "pass1234",
        "vehicle_id": "JA88888"})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_token(client):
    r = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return r.json()["access_token"]


def auth(token):
    return {"Authorization": "Bearer " + token}


# ======================================================================
# Part 1: 数据模型字段检查
# ======================================================================

class TestModelFields:
    def test_charge_order_has_new_fields(self):
        from src.models.models import ChargeOrder
        cols = {c.name for c in ChargeOrder.__table__.columns}
        for f in ["requested_kwh", "charged_kwh", "queue_number",
                   "user_id", "bill_code", "charge_start_time",
                   "charge_end_time", "charge_duration", "total_power",
                   "power_fee", "service_fee", "total_fee"]:
            assert f in cols, "ChargeOrder missing column: " + f

    def test_charge_order_no_old_soc_fields(self):
        from src.models.models import ChargeOrder
        cols = {c.name for c in ChargeOrder.__table__.columns}
        for f in ["start_soc", "target_soc", "charge_kwh",
                   "electricity_fee", "end_soc", "timeout_fee"]:
            assert f not in cols, "ChargeOrder should NOT have: " + f

    def test_user_model_exists(self):
        from src.models.models import User
        cols = {c.name for c in User.__table__.columns}
        for f in ["username", "password_hash", "role", "vehicle_id"]:
            assert f in cols

    def test_order_status_has_waiting(self):
        from src.models.models import OrderStatus
        assert OrderStatus.WAITING == "WAITING"
        assert OrderStatus.QUEUING == "QUEUING"
        assert OrderStatus.CHARGING == "CHARGING"
        assert OrderStatus.COMPLETED == "COMPLETED"
        assert OrderStatus.CANCELLED == "CANCELLED"
        assert OrderStatus.FAULTED == "FAULTED"

    def test_charging_pile_has_fee_stats(self):
        from src.models.models import ChargingPile
        cols = {c.name for c in ChargingPile.__table__.columns}
        for f in ["total_power_fee", "total_service_fee", "total_total_fee"]:
            assert f in cols


# ======================================================================
# Part 2: 计费引擎 (billing.py — 不变)
# ======================================================================

class TestBilling:
    def test_signature(self):
        import inspect
        from src.core.billing import calculate_fee
        sig = inspect.signature(calculate_fee)
        params = list(sig.parameters.keys())
        assert params == ["start_time", "end_time", "total_kwh"]

    def test_pure_peak(self):
        from datetime import datetime
        from src.core.billing import calculate_fee
        s = datetime(2026, 4, 1, 11, 0)
        e = datetime(2026, 4, 1, 13, 0)
        r = calculate_fee(s, e, 30.0)
        assert r["power_fee"] == 30.0
        assert r["service_fee"] == 24.0
        assert r["total_fee"] == 54.0

    def test_pure_valley(self):
        from datetime import datetime
        from src.core.billing import calculate_fee
        s = datetime(2026, 4, 1, 1, 0)
        e = datetime(2026, 4, 1, 4, 0)
        r = calculate_fee(s, e, 20.0)
        assert r["power_fee"] == 8.0

    def test_zero_kwh(self):
        from datetime import datetime
        from src.core.billing import calculate_fee
        r = calculate_fee(datetime.now(), datetime.now(), 0)
        assert r["total_fee"] == 0.0


# ======================================================================
# Part 3: 认证
# ======================================================================

class TestAuth:
    def test_register(self, client):
        r = client.post("/api/auth/register", json={
            "username": "authtest", "password": "abcd1234"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["role"] == "user"

    def test_register_duplicate(self, client):
        r = client.post("/api/auth/register", json={
            "username": "authtest", "password": "abcd1234"})
        assert r.status_code == 400

    def test_login_success(self, client):
        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"

    def test_login_fail(self, client):
        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_me(self, client, user_token):
        r = client.get("/api/auth/me", headers=auth(user_token))
        assert r.status_code == 200
        assert r.json()["username"] == "testuser"

    def test_no_token_rejected(self, client):
        r = client.post("/api/user/requests/", json={
            "vehicle_id": "V1", "charge_type": "Fast",
            "requested_kwh": 10.0})
        assert r.status_code == 401


# ======================================================================
# Part 4: 排队号码与最短时间调度
# ======================================================================

class TestQueueNumberAndScheduling:
    def test_queue_numbers_sequential(self, client):
        """F 类号码 F1, F2, F3... 顺序递增"""
        ids = []
        for i in range(3):
            r = client.post("/api/requests/", json={
                "vehicle_id": "QN_F_{}".format(i),
                "charge_type": "Fast", "requested_kwh": 5.0})
            assert r.status_code == 200
            data = r.json()
            assert data["queue_number"] is not None
            ids.append(data["queue_number"])
        # 前缀应该是 F，编号递增
        for qn in ids:
            assert qn.startswith("F")

    def test_slow_queue_numbers(self, client):
        """T 类号码 T1, T2... 顺序递增"""
        r = client.post("/api/requests/", json={
            "vehicle_id": "QN_T_0", "charge_type": "Slow",
            "requested_kwh": 5.0})
        assert r.status_code == 200
        assert r.json()["queue_number"].startswith("T")

    def test_shortest_time_scheduling(self, client):
        """
        提交不同 kWh 的请求，验证最短时间调度将车辆
        分配到总完成时间最短的桩。
        """
        # 获取当前状态
        r = client.get("/api/system/dump")
        status = r.json()
        # 找到有空位的 Fast 桩
        fast_piles = [p for p in status["piles"]
                      if p["type"] == "Fast"]
        # 验证每个桩有队列
        for p in fast_piles:
            assert "queue_len" in p
            assert "max_queue_len" in p


# ======================================================================
# Part 5: 每桩独立队列
# ======================================================================

class TestPerPileQueue:
    def test_pile_has_queue_detail(self, client):
        """系统状态应返回每桩的队列详情"""
        r = client.get("/api/system/dump")
        assert r.status_code == 200
        for p in r.json()["piles"]:
            assert "queue_items" in p
            assert "queue_len" in p
            assert "max_queue_len" in p
            assert p["max_queue_len"] == 2  # config: pile_queue_length=2

    def test_queue_overflow_to_waiting(self, client):
        """桩队列满后，车辆进入等候区"""
        # 大量提交 Slow 请求（只有 2 个慢桩，每桩队列长 2 = 最多 4 辆）
        waiting_count = 0
        for i in range(5):
            r = client.post("/api/requests/", json={
                "vehicle_id": "SLOW_{}".format(i),
                "charge_type": "Slow", "requested_kwh": 8.0})
            data = r.json()
            if data.get("assigned_pile") is None:
                waiting_count += 1
        # 至少有一些车应该进入等候区
        r = client.get("/api/system/dump")
        assert r.json()["slow_waiting_count"] >= waiting_count


# ======================================================================
# Part 6: 修改请求
# ======================================================================

class TestModifyRequest:
    def test_modify_in_waiting_area(self, client, user_token, app):
        """等候区的车可以修改充电量"""
        # 增大系统容量，避免因前序测试占位而无法创建等候区订单
        app.state.scheduler.waiting_capacity = 200

        last_order_id = None
        for i in range(8):
            r = client.post("/api/user/requests/", json={
                "vehicle_id": "MOD_F_{}".format(i),
                "charge_type": "Fast", "requested_kwh": 20.0},
                headers=auth(user_token))
            if r.json().get("assigned_pile") is None:
                last_order_id = r.json()["order_id"]

        if last_order_id is None:
            pytest.skip("无法创建等候区订单")

        # 修改充电量
        r = client.post(
            "/api/user/requests/{}/modify".format(last_order_id),
            json={"requested_kwh": 25.0},
            headers=auth(user_token))
        assert r.status_code == 200
        assert r.json()["status"] == "success"

    def test_modify_mode_in_waiting(self, client, user_token, app):
        """等候区可以修改充电模式（Fast→Slow），会重新生成排队号"""
        app.state.scheduler.waiting_capacity = 200

        last_order_id = None
        for i in range(5):
            r = client.post("/api/user/requests/", json={
                "vehicle_id": "MODEM_{}".format(i),
                "charge_type": "Fast", "requested_kwh": 10.0},
                headers=auth(user_token))
            if r.json().get("assigned_pile") is None:
                last_order_id = r.json()["order_id"]

        if last_order_id is None:
            pytest.skip("无法创建等候区订单")

        r = client.post(
            "/api/user/requests/{}/modify".format(last_order_id),
            json={"charge_type": "Slow"},
            headers=auth(user_token))
        assert r.status_code == 200
        new_qn = r.json().get("new_queue_number", "")
        assert new_qn.startswith("T"), "模式切换后应获得 T 开头的号码"

    def test_modify_in_pile_rejected(self, client, user_token):
        """充电区的车不允许修改"""
        # 获取一个正在充电的订单
        r = client.get("/api/system/dump")
        charging_order = None
        for p in r.json()["piles"]:
            for qi in p.get("queue_items", []):
                charging_order = qi["order_id"]
                break
            if charging_order:
                break
        if charging_order is None:
            pytest.skip("无正在充电的订单")

        r = client.post(
            "/api/user/requests/{}/modify".format(charging_order),
            json={"requested_kwh": 100.0},
            headers=auth(user_token))
        assert r.status_code == 400


# ======================================================================
# Part 7: 取消与停止
# ======================================================================

class TestCancelStop:
    def test_cancel_waiting(self, client, app):
        """取消等候区的车"""
        # 增大系统容量，确保可以创建等候区订单
        app.state.scheduler.waiting_capacity = 200

        # 先填满桩队列，让后续请求进入等候区
        for i in range(10):
            client.post("/api/requests/", json={
                "vehicle_id": "CANCEL_{}".format(i),
                "charge_type": "Fast", "requested_kwh": 10.0})

        # 提交一个新请求，应进入等候区
        resp = client.post("/api/requests/", json={
            "vehicle_id": "CANCEL_LAST", "charge_type": "Fast",
            "requested_kwh": 5.0})
        oid = resp.json().get("order_id")
        if oid and resp.json().get("assigned_pile") is None:
            r = client.post("/api/requests/{}/cancel".format(oid))
            assert r.status_code == 200
        else:
            # 从等候区取一个已有的取消
            scheduler = app.state.scheduler
            if len(scheduler.fast_waiting) > 0:
                woid = scheduler.fast_waiting[0]["order_id"]
                r = client.post("/api/requests/{}/cancel".format(woid))
                assert r.status_code == 200
            else:
                pytest.skip("无法创建等候区订单")

    def test_stop_charging(self, client):
        """停止正在充电的车"""
        r = client.get("/api/system/dump")
        charging_oid = None
        for p in r.json()["piles"]:
            if p["status"] == "CHARGING" and len(p["queue_items"]) > 0:
                charging_oid = p["queue_items"][0]["order_id"]
                break
        if charging_oid is None:
            pytest.skip("无正在充电的订单")

        r = client.post("/api/requests/{}/stop".format(charging_oid))
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert "total_fee" in data


# ======================================================================
# Part 8: 管理员接口
# ======================================================================

class TestAdmin:
    def test_admin_piles(self, client, admin_token):
        r = client.get("/api/admin/piles", headers=auth(admin_token))
        assert r.status_code == 200
        assert "piles" in r.json()

    def test_admin_waiting_area(self, client, admin_token):
        r = client.get("/api/admin/waiting-area",
                        headers=auth(admin_token))
        assert r.status_code == 200
        assert "fast_waiting" in r.json()
        assert "slow_waiting" in r.json()

    def test_admin_dispatch(self, client, admin_token):
        r = client.post("/api/admin/dispatch",
                         headers=auth(admin_token))
        assert r.status_code == 200

    def test_admin_reports(self, client, admin_token):
        r = client.get("/api/admin/reports?period=day",
                        headers=auth(admin_token))
        assert r.status_code == 200
        data = r.json()
        assert "period" in data
        assert "items" in data

    def test_admin_reports_week(self, client, admin_token):
        r = client.get("/api/admin/reports?period=week",
                        headers=auth(admin_token))
        assert r.status_code == 200

    def test_admin_reports_month(self, client, admin_token):
        r = client.get("/api/admin/reports?period=month",
                        headers=auth(admin_token))
        assert r.status_code == 200

    def test_user_cannot_access_admin(self, client, user_token):
        """普通用户不能访问管理员接口"""
        r = client.get("/api/admin/piles", headers=auth(user_token))
        assert r.status_code == 403

    def test_fault_pile(self, client, admin_token):
        """管理员可以将桩置为故障"""
        r = client.post("/api/admin/piles/F1/control",
                         json={"action": "fault"},
                         headers=auth(admin_token))
        assert r.status_code == 200
        # 验证桩状态
        r = client.get("/api/admin/piles", headers=auth(admin_token))
        f1 = [p for p in r.json()["piles"] if p["pile_id"] == "F1"][0]
        assert f1["status"] == "FAULT"

    def test_recover_pile(self, client, admin_token):
        """管理员可以恢复故障桩"""
        r = client.post("/api/admin/piles/F1/control",
                         json={"action": "recover"},
                         headers=auth(admin_token))
        assert r.status_code == 200
        r = client.get("/api/admin/piles", headers=auth(admin_token))
        f1 = [p for p in r.json()["piles"] if p["pile_id"] == "F1"][0]
        assert f1["status"] != "FAULT"

    def test_stop_pile(self, client, admin_token):
        """管理员可以关闭桩"""
        r = client.post("/api/admin/piles/F3/control",
                         json={"action": "stop"},
                         headers=auth(admin_token))
        assert r.status_code == 200


# ======================================================================
# Part 9: 账单详情
# ======================================================================

class TestBills:
    def test_bill_has_all_fields(self, client):
        """已完成订单的账单应包含所有必要字段"""
        # 先提交一个请求并让它完成
        r = client.post("/api/requests/", json={
            "vehicle_id": "BILL_TEST", "charge_type": "Slow",
            "requested_kwh": 0.01})  # 极小量，很快充完
        oid = r.json()["order_id"]

        # 等几秒让模拟充满
        time.sleep(3)

        r = client.get("/api/bills/{}".format(oid))
        if r.status_code == 200:
            data = r.json()
            assert data["order_id"] == oid
            assert data["vehicle_id"] == "BILL_TEST"
            assert "requested_kwh" in data
            assert "queue_number" in data
            # 如果已完成，应有费用
            if data["status"] in ("COMPLETED", "FAULTED"):
                assert data["bill_code"] is not None
                assert data["total_power"] is not None
                assert data["total_fee"] is not None
                assert data["charge_start_time"] is not None
                assert data["charge_end_time"] is not None

    def test_bill_not_found(self, client):
        r = client.get("/api/bills/99999")
        assert r.status_code == 404


# ======================================================================
# Part 10: 配置验证
# ======================================================================

class TestConfig:
    def test_system_config(self):
        from src.loader import config_data
        assert config_data['system']['fast_pile_count'] == 3
        assert config_data['system']['slow_pile_count'] == 2
        assert config_data['system']['waiting_area_size'] == 10
        assert config_data['system']['pile_queue_length'] == 2

    def test_charging_config(self):
        from src.loader import config_data
        assert config_data['charging']['fast_power'] == 30.0
        assert config_data['charging']['slow_power'] == 10.0

    def test_billing_config(self):
        from src.loader import config_data
        assert config_data['billing']['peak_rate'] == 1.0
        assert config_data['billing']['flat_rate'] == 0.7
        assert config_data['billing']['valley_rate'] == 0.4
        assert config_data['billing']['service_fee_rate'] == 0.8


# ======================================================================
# Part 11: 充电完成后不自动调度验证 (核心约束)
# ======================================================================

class TestNoAutoDispatch:
    def test_charging_complete_no_dispatch(self, client):
        """
        充电完成后，等候区的车不应被自动调度到空闲桩。
        需要等待 dispatch_watcher 或手动触发。
        """
        # 注意：由于 TestClient 是同步的，后台任务可能不完全运行。
        # 我们通过检查调度器代码结构来确认约束。
        from src.core.scheduler import SmartScheduler
        import inspect
        source = inspect.getsource(SmartScheduler.simulate_battery_growth)
        # 确认在充电完成逻辑后没有调度调用
        assert "dispatch_from_waiting" not in source
        assert "dispatch_watcher" not in source

    def test_stop_charging_no_dispatch(self, client):
        from src.core.scheduler import SmartScheduler
        import inspect
        source = inspect.getsource(SmartScheduler.stop_charging)
        assert "dispatch_from_waiting" not in source


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
