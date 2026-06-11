#!/usr/bin/env python3
"""
G9 — 扩展调度策略端到端测试 (照 G8 模式)
========================================

启动后:
  1) 杀掉旧进程 + 重置 DB
  2) 后台启动 uvicorn
  3) 自动开浏览器 + 等用户登录切策略
  4) 自动注册 11 辆 + 设置 pile_queue_length=1
  5) 自动提交 8 fast + 3 slow 充电请求
  6) 启动虚拟时钟 (ratio 默认 1, 即 1 真实秒 = 1 虚拟分钟)
  7) 循环触发调度 (每 2s 一次, 用用户选的策略), 直到等候区清空或超时
  8) 打印每辆车状态
  9) 服务器保留运行, 按 Ctrl+C 退出

使用方式:
    python scripts/g9_test.py [--ratio RATIO] [--port PORT]
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

# 强制 UTF-8 输出 (Windows console 默认 GBK 会乱码)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_FILE = PROJECT_ROOT / "charging.db"
G9_DB_DIR = PROJECT_ROOT / "g9_dbs"
G9_DB_DIR.mkdir(exist_ok=True)

ADMIN_USER = "admin"
ADMIN_PASS = "admin123"
TEST_USER = "g9_tester"
TEST_PASS = "test1234"

VTIME_BASE = datetime(2026, 6, 5, 6, 0, 0)


# ============================================================
#  与 g8_test.py 复用的工具
# ============================================================

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def get_lan_ip() -> str:
    """获取本机局域网 IP (供多 PC 访问)"""
    import re
    try:
        # 用 socket 连外网来反查本机 IP (不实际发数据)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))  # 阿里 DNS
        ip = s.getsockname()[0]
        s.close()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # 备选: 遍历网络接口
    try:
        import subprocess as _sp
        out = _sp.run(["ipconfig"], capture_output=True,
                      text=True, timeout=5, encoding="gbk", errors="ignore")
        for line in out.stdout.splitlines():
            m = re.search(r"IPv4[^\d]+(\d+\.\d+\.\d+\.\d+)", line)
            if m and not m.group(1).startswith("127.169"):
                return m.group(1)
    except Exception:
        pass
    return "127.0.0.1"


def kill_process_on_port(port: int):
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True,
            text=True, timeout=10)
        pids = set()
        for line in result.stdout.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 5 and f":{port}" in parts[1] \
                    and parts[3] == "LISTENING" \
                    and parts[4].isdigit() and int(parts[4]) > 0:
                pids.add(parts[4])
        for pid in pids:
            subprocess.run(
                ["taskkill", "/f", "/pid", pid],
                capture_output=True, timeout=5)
    except Exception:
        pass


def cleanup_database(db_path: Path = DB_FILE) -> bool:
    """删除指定 DB 文件"""
    if not db_path.exists():
        return True
    for attempt in range(10):
        try:
            os.remove(db_path)
            return True
        except PermissionError:
            time.sleep(1.0)
    return False


def make_fresh_db(label: str) -> Path:
    """为每个场景生成全新 DB 路径"""
    p = G9_DB_DIR / f"g9_{label}_{int(time.time())}.db"
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    return p


async def wait_for_server(client: httpx.AsyncClient, timeout: float = 45):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = await client.get("/api/admin/piles")
            if r.status_code in (200, 401):
                return True
        except (httpx.ConnectError, httpx.ConnectTimeout):
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("服务器启动超时")


# ============================================================
#  G9 客户端 (最小化)
# ============================================================

class G9Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self.admin_token = ""
        self.user_token = ""
        self.vehicle_order_map = {}  # vid -> order_id

    async def start(self):
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, connect=10.0))

    async def close(self):
        if self.client:
            await self.client.aclose()

    def _h_admin(self):
        return {"Authorization": f"Bearer {self.admin_token}"}

    def _h_user(self):
        return {"Authorization": f"Bearer {self.user_token}"}

    async def login_admin(self):
        r = await self.client.post("/api/auth/login", json={
            "username": ADMIN_USER, "password": ADMIN_PASS})
        assert r.status_code == 200, f"管理员登录失败: {r.text}"
        self.admin_token = r.json()["access_token"]

    async def register_user(self):
        r = await self.client.post("/api/auth/register", json={
            "username": TEST_USER, "password": TEST_PASS})
        if r.status_code == 400:
            r = await self.client.post("/api/auth/login", json={
                "username": TEST_USER, "password": TEST_PASS})
        assert r.status_code == 200, f"用户登录失败: {r.text}"
        self.user_token = r.json()["access_token"]

    async def register_vehicle(self, vid: str, cap: float = 100.0,
                               current: float = 0.0):
        r = await self.client.post("/api/user/vehicles", json={
            "vehicle_id": vid, "battery_capacity_kwh": cap,
            "current_kwh": current}, headers=self._h_user())
        if r.status_code == 400:
            await self.client.put(f"/api/user/vehicles/{vid}", json={
                "current_kwh": current}, headers=self._h_user())

    async def submit(self, vid: str, charge_type: str, kwh: float):
        r = await self.client.post("/api/user/requests/", json={
            "vehicle_id": vid, "charge_type": charge_type,
            "requested_kwh": kwh}, headers=self._h_user())
        if r.status_code != 200:
            return None
        data = r.json()
        self.vehicle_order_map[vid] = data.get("order_id")
        return data

    async def setup_clock(self, ratio: float):
        await self.client.post("/api/admin/clock/pause",
                                headers=self._h_admin())
        r = await self.client.put("/api/admin/clock", json={
            "datetime": VTIME_BASE.strftime("%Y-%m-%d %H:%M:%S"),
            "ratio": ratio}, headers=self._h_admin())
        assert r.status_code == 200, f"时钟设置失败: {r.text}"

    async def start_clock(self):
        await self.client.post("/api/admin/clock/start",
                                headers=self._h_admin())

    async def dispatch_fifo(self):
        """原 FIFO 调度入口"""
        r = await self.client.post("/api/admin/dispatch",
                                    headers=self._h_admin())
        return r.json()

    async def dispatch_batch(self):
        """新策略调度入口"""
        r = await self.client.post("/api/admin/dispatch-policy",
                                    json={"policy": "batch_min_total"},
                                    headers=self._h_admin())
        return r.json()

    async def get_pile_status(self) -> list:
        r = await self.client.get("/api/admin/piles",
                                   headers=self._h_admin())
        if r.status_code != 200:
            return []
        return r.json().get("piles", [])

    async def get_waiting(self) -> dict:
        """API 直接返回 WaitingAreaResponse 对象, 不再包一层"""
        r = await self.client.get("/api/admin/waiting-area",
                                   headers=self._h_admin())
        if r.status_code != 200:
            return {"fast_waiting": [], "slow_waiting": []}
        return r.json()


# ============================================================
#  主流程 (完全照 G8 模式)
# ============================================================

async def main_async(args):
    """
    G9 主流程 (完全照 G8 模式):
      1) 杀端口 + 重置 DB
      2) 后台启动 uvicorn (在 0.0.0.0:{port})
      3) 等待 server up
      4) 打印 URL + 自动开浏览器 + 提示用户登录切策略
      5) 阻塞等用户按 Enter
      6) 自动提交测试车辆 (用户只管看前端)
      7) 触发调度 (用用户已选的策略)
      8) 等几秒, 让充电推进, 收集结果
      9) 打印结果
     10) 服务器保留运行 (Ctrl+C 退出)
    """
    port = args.port or 8000
    lan_ip = get_lan_ip()

    # 1) 杀端口 + 重置 DB
    kill_process_on_port(port)
    await asyncio.sleep(1)
    db_path = make_fresh_db("g9")

    # 2) 启动 uvicorn (后台, 单实例)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONUNENCODED"] = "1"  # 注: 不是 PYTHONUNBUFFERED, 看下面
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["SCS_DB_PATH"] = str(db_path)
    env["SCS_DISABLE_WATCHER"] = "1"   # 禁用后台 watcher, 让我们手动调度的策略能真正生效
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app",
         "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=open(PROJECT_ROOT / "server_g9.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    try:
        # 3) 等待 server up
        async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=httpx.Timeout(15.0)) as probe:
            await wait_for_server(probe)
        print(f"  [OK] uvicorn 已启动 (DB={db_path.name})")

        # 4) 打印 URL + 打开浏览器 + 提示用户操作
        url = f"http://127.0.0.1:{port}/admin.html"
        lan_url = f"http://{lan_ip}:{port}/admin.html" if lan_ip != "127.0.0.1" else None
        print()
        print("  " + "=" * 58)
        print("  |  请在浏览器中完成以下操作 (已自动打开):")
        print("  |")
        print(f"  |    1. 访问  {url}")
        if lan_url:
            print(f"  |      或  {lan_url}  (其他 PC)")
        print("  |    2. 登录 (admin / admin123)")
        print("  |    3. 切换到 [系统设置] Tab")
        print("  |    4. 在'扩展调度策略'卡片中选择: 单次调度总充电时长最短")
        print("  |    5. 点击 [应用并立即触发一次] (可暂不点, 之后 G9 会触发)")
        print("  |")
        print("  |  完成上述操作后, 回到本窗口按 Enter 继续...")
        print("  " + "=" * 58)
        print()
        if sys.platform == "win32" and not args.no_browser:
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                print(f"  [BROWSER] 已自动打开浏览器 → {url}")
            except Exception as e:
                print(f"  [WARN] 自动打开失败: {e}; 请手动复制上面 URL")

        # 5) 阻塞等用户按 Enter
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("  >>> 按 Enter 继续 (G9 将自动提交测试车辆)...\n"))
        print()

        # 6) 自动提交测试车辆 (注册 + 提交充电请求)
        print("  [STEP 1/4] 管理员登录 ...")
        tc = G9Client(f"http://127.0.0.1:{port}")
        await tc.start()
        try:
            await tc.login_admin()
            await tc.register_user()
            print("  [STEP 1/4] 注册 11 辆测试车辆 ...")
            for i in range(1, 12):
                await tc.register_vehicle(f"V{i}")

            print("  [STEP 1/4] 设置桩队列长度=1 (制造等候区) ...")
            r = await tc.client.put(
                "/api/admin/system-params",
                json={"pile_queue_length": 1},
                headers=tc._h_admin())
            assert r.status_code == 200, f"改桩队列长度失败: {r.text}"

            # 【关键】先设置虚拟时钟, 再提交车辆
            # 否则 V1-V3 的 charge_start_time 会是真实世界时间 (而非虚拟时间),
            # 导致 finish 时 start > end, calculate_fee 返回 0
            print("  [STEP 2/4] 设置虚拟时钟到 06:00 (ratio={}) ...".format(args.ratio))
            await tc.setup_clock(ratio=args.ratio)

            print("  [STEP 2/4] 提交 11 辆充电请求 (8 fast + 3 slow) ...")
            plan = [
                # 8 辆 fast: 前 3 进 3 个 fast 桩 (各 1 个车位), 后 5 进等候区
                ("V1", "Fast", 15.0),
                ("V2", "Fast", 15.0),
                ("V3", "Fast", 15.0),
                ("V4", "Fast", 10.0),   # 等候区, kWh 最小
                ("V5", "Fast", 20.0),   # 等候区
                ("V6", "Fast", 12.0),   # 等候区
                ("V7", "Fast", 18.0),   # 等候区
                ("V8", "Fast", 14.0),   # 等候区
                # 3 辆 slow: 前 2 进 2 个 slow 桩, 后 1 进等候区
                ("V9",  "Slow", 25.0),
                ("V10", "Slow", 25.0),
                ("V11", "Slow", 15.0),  # 等候区
            ]
            for vid, ctype, kwh in plan:
                await tc.submit(vid, ctype, kwh)
            print(f"  [STEP 2/4] 11 辆已提交 (3 fast + 2 slow 进桩, 6 进等候区)")

            # 7) 启动时钟 + 循环调度 (每 2s 一次, 把新空出来的桩位填上)
            print(f"  [STEP 3/4] 启动虚拟时钟 (从 06:00 开始推进) ...")
            await tc.start_clock()

            # 读当前策略 (用户在 UI 里选的那个)
            r = await tc.client.get("/api/admin/dispatch-policy",
                                     headers=tc._h_admin())
            current_policy = r.json().get("policy", "fifo") if r.status_code == 200 else "fifo"
            print(f"  [STEP 3/4] 用户在 UI 选的策略: {current_policy}")

            print(f"  [STEP 3/4] 阶段 A: 循环调度 (每 3s 一次, 最多 {args.max_dispatch_rounds} 轮) ...")
            print(f"  [STEP 3/4] (等桩上的车充完, 把等候区车辆补位上去)")
            print(f"  [STEP 3/4] (15kWh fast @ ratio={args.ratio} 需 ~{int(15/30*60/args.ratio)}s 充完, "
                  f"25kWh slow 需 ~{int(25/10*60/args.ratio)}s)")
            for round_i in range(1, args.max_dispatch_rounds + 1):
                await asyncio.sleep(3.0)
                # 触发一次调度 (用用户选的策略)
                if current_policy == "batch_min_total":
                    res = await tc.dispatch_batch()
                else:
                    res = await tc.dispatch_fifo()
                # 检查等候区是否还有车
                waiting_info = await tc.get_waiting()
                waiting_count = (len(waiting_info.get("fast_waiting", []))
                                  + len(waiting_info.get("slow_waiting", [])))
                n_done = res.get("result", {}).get("assignments_count", 0) if isinstance(res, dict) else 0
                print(f"  [调度轮次 {round_i:2d}] 触发 {current_policy} → "
                      f"本轮分配 {n_done} 辆, 等候区剩 {waiting_count} 辆")
                if waiting_count == 0:
                    print(f"  [STEP 3/4] 阶段 A 完成: 等候区已清空")
                    break
            else:
                print(f"  [STEP 3/4] 阶段 A 达到 {args.max_dispatch_rounds} 轮上限, 强制结束")

            # 阶段 B: 等所有车都充电完成 (不等车在等候区, 等所有订单都 COMPLETED)
            print(f"  [STEP 3/4] 阶段 B: 等所有车充电完毕 (每 3s 查状态, 最多 {args.max_wait_rounds} 轮) ...")
            for round_i in range(1, args.max_wait_rounds + 1):
                await asyncio.sleep(3.0)
                # 查询所有订单
                r = await tc.client.get("/api/admin/orders",
                                         headers=tc._h_admin())
                if r.status_code != 200:
                    continue
                orders = r.json().get("orders", [])
                n_total = len(orders)
                n_completed = sum(1 for o in orders if o.get("status") == "COMPLETED")
                n_charging = sum(1 for o in orders if o.get("status") == "CHARGING")
                n_other = n_total - n_completed - n_charging
                print(f"  [等待轮次 {round_i:2d}] {n_completed}/{n_total} 已完成, "
                      f"{n_charging} 充电中, {n_other} 其他")
                if n_total > 0 and n_completed == n_total:
                    print(f"  [STEP 3/4] 阶段 B 完成: 所有 {n_total} 辆车均已充电完毕")
                    break
            else:
                print(f"  [STEP 3/4] 阶段 B 达到 {args.max_wait_rounds} 轮上限, "
                      f"还有 {n_charging} 辆车未完成 (可在浏览器继续观察)")
            r = await tc.client.get("/api/admin/orders",
                                     headers=tc._h_admin())
            completion_map = {}
            if r.status_code == 200:
                for o in r.json().get("orders", []):
                    completion_map[o["vehicle_id"]] = {
                        "pile_id": o.get("pile_id"),
                        "charged_kwh": o.get("charged_kwh"),
                        "status": o.get("status"),
                        "started_at": o.get("started_at"),
                        "finished_at": o.get("finished_at"),
                    }
        finally:
            await tc.close()

        # 9) 打印结果
        print(f"\n{'='*60}")
        print(f"  G9 测试结果 (策略: {current_policy})")
        print(f"{'='*60}")
        print(f"  {'车辆':<6}{'桩':<6}{'状态':<12}{'已充(kWh)':<12}{'开始':<22}{'完成':<22}")
        for vid in sorted(completion_map.keys()):
            info = completion_map[vid]
            pile = info.get("pile_id") or "-"
            status = info.get("status") or "-"
            kwh = info.get("charged_kwh") or 0
            start = info.get("started_at") or "-"
            finish = info.get("finished_at") or "-"
            print(f"  {vid:<6}{pile:<6}{status:<12}{kwh:<12.2f}{start:<22}{finish:<22}")

        n_completed = sum(1 for v in completion_map.values()
                          if v.get("status") == "COMPLETED")
        n_charging = sum(1 for v in completion_map.values()
                         if v.get("status") == "CHARGING")
        n_in_pile = sum(1 for v in completion_map.values() if v.get("pile_id"))
        print(f"\n  汇总: {len(completion_map)} 辆车, "
              f"{n_completed} 已完成, {n_charging} 充电中, "
              f"{n_in_pile} 已分配到桩")

        # 落盘
        report = {
            "policy": current_policy,
            "ratio": args.ratio,
            "completion_map": completion_map,
            "summary": {
                "total": len(completion_map),
                "completed": n_completed,
                "charging": n_charging,
                "in_pile": n_in_pile,
            }
        }
        out = PROJECT_ROOT / "scripts" / "g9_results.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"  报告已落盘: {out}")

    finally:
        # 10) 保留服务器 (Ctrl+C 退出)
        print()
        print("=" * 60)
        print(f"  [KEEP-ALIVE] 服务器保留运行, 可继续在浏览器操作/重置")
        print(f"    本机: {url}")
        if lan_ip != "127.0.0.1":
            print(f"    局域网: {lan_url}")
        print(f"    日志: server_g9.log")
        print()
        print(f"  [车辆端] 打开 {url.replace('admin', '')} 或 {url.split('/admin')[0]}/")
        print(f"           输入 V1 (或 V2..V11) 注册, 可查看该车的账单 + 详单")
        print(f"           (前端每 3 秒自动刷新详单)")
        print()
        print(f"  [账单界面] 报表 / 订单管理 / 详单 Tab 都会每 3 秒自动刷新")
        print(f"  按 Ctrl+C 关闭")
        print("=" * 60)
        try:
            while True:
                await asyncio.sleep(60)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        try:
            server.terminate()
            server.wait(timeout=5)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass
        kill_process_on_port(port)


def main():
    p = argparse.ArgumentParser(
        description="G9 扩展调度策略端到端测试 (FIFO/BATCH 都支持, 多 PC 访问)")
    p.add_argument("--ratio", type=float, default=2.0,
                   help="虚拟时钟倍率 (默认2, 与 G8 一致, 即 1真实秒=2虚拟分钟)")
    p.add_argument("--port", type=int, default=8000,
                   help="服务器端口 (默认8000)")
    p.add_argument("--no-browser", action="store_true",
                   help="不自动打开浏览器 (调试用)")
    p.add_argument("--max-dispatch-rounds", type=int, default=20,
                   help="阶段 A 调度循环最多跑多少轮 (每轮 3s, 默认 20 = 60s)")
    p.add_argument("--max-wait-rounds", type=int, default=40,
                   help="阶段 B 等待所有车完成最多跑多少轮 (每轮 3s, 默认 40 = 120s)")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n  已被用户中断")
    except Exception as e:
        print(f"\n  [FATAL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
