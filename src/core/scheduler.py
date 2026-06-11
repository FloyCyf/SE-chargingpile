import asyncio
import math
import random
import string
from datetime import datetime
from typing import Dict, List, Optional
from sqlalchemy import select
from src.api.schemas import ChargeRequest
from src.core.clock import VirtualClock
from src.core.billing import calculate_fee
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder, OrderStatus, Vehicle, PileStatusLog, Bill, BillDetail


# ---------------------------------------------------------------------------
#  充电桩 — 内存运行态
# ---------------------------------------------------------------------------

class ChargingPile:
    def __init__(self, pile_id: str, pile_type: str, power: float,
                 max_queue_length: int):
        self.pile_id = pile_id
        self.type = pile_type          # 'Fast' / 'Slow'
        self.power = power             # kW (30.0 / 10.0)
        self.status = "IDLE"           # IDLE / CHARGING / FAULT
        self.max_queue_length = max_queue_length

        # 每桩独立排队队列; position 0 = 正在充电的车
        self.queue: List[dict] = []

        # 累计统计（内存镜像）
        self.total_charge_count: int = 0
        self.total_charge_duration: float = 0.0
        self.total_charge_amount: float = 0.0
        self.total_power_fee: float = 0.0
        self.total_service_fee: float = 0.0
        self.total_total_fee: float = 0.0

    @property
    def has_space(self) -> bool:
        return self.status != "FAULT" and len(self.queue) < self.max_queue_length

    @property
    def is_idle(self) -> bool:
        return self.status == "IDLE" and len(self.queue) == 0

    def remaining_time_hours(self) -> float:
        """该桩队列中所有车辆的剩余充电总时长 (小时)"""
        total = 0.0
        for i, car in enumerate(self.queue):
            if i == 0:
                remaining_kwh = max(0, car["requested_kwh"] - car["charged_kwh"])
            else:
                remaining_kwh = car["requested_kwh"]
            total += remaining_kwh / self.power
        return total


# ---------------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------------

def _generate_bill_code() -> str:
    rand_chars = ''.join(random.choices(
        string.ascii_uppercase + string.digits, k=6))
    return "BILL" + datetime.now().strftime("%Y%m%d%H%M%S") + rand_chars


def _queue_number_sort_key(qn: str) -> tuple:
    """F1 → ('F', 1),  T12 → ('T', 12)"""
    prefix = qn[0]
    num = int(qn[1:]) if len(qn) > 1 else 0
    return (prefix, num)


# ---------------------------------------------------------------------------
#  智能调度器
# ---------------------------------------------------------------------------

class SmartScheduler:
    def __init__(self, config: dict):
        self.config = config

        # 系统参数
        self.waiting_capacity = config['system'].get(
            'waiting_area_size',
            config['system'].get('waiting_area_capacity', 10))
        self.pile_queue_length = config['system'].get('pile_queue_length', 3)
        self.fast_power = config.get('charging', {}).get('fast_power', 30.0)
        self.slow_power = config.get('charging', {}).get('slow_power', 10.0)
        self.battery_capacity = config.get('billing', {}).get(
            'battery_capacity_kwh', 60.0)

        # 虚拟时钟与时间加速参数
        self.clock = VirtualClock(config)
        self.virtual_ratio = config['simulation'].get(
            'virtual_minutes_per_real_second', 1)

        self.lock = asyncio.Lock()

        # 初始化充电桩
        fast_count = config['system'].get(
            'fast_pile_count',
            config['system'].get('fast_charging_piles', 3))
        slow_count = config['system'].get(
            'slow_pile_count',
            config['system'].get('slow_charging_piles', 2))

        self.piles: List[ChargingPile] = []
        for i in range(fast_count):
            self.piles.append(ChargingPile(
                f"F{i+1}", "Fast", self.fast_power, self.pile_queue_length))
        for i in range(slow_count):
            self.piles.append(ChargingPile(
                f"T{i+1}", "Slow", self.slow_power, self.pile_queue_length))

        # 等候区（两个全局 FIFO 列表）
        self.fast_waiting: List[dict] = []
        self.slow_waiting: List[dict] = []
        self.pending_requests: List[dict] = []
        self.fast_fault_waiting: List[dict] = []
        self.slow_fault_waiting: List[dict] = []
        self.fault_recover_tasks: dict[str, asyncio.Task] = {}

        # 排队号计数器
        self.fast_counter: int = 0
        self.slow_counter: int = 0

        # 故障调度锁
        self.numbering_paused: bool = False

        # 动态优先级权重 P_i = alpha*(1-SOC) + beta*W_type + gamma*ln(t_wait+1)
        priority_cfg = config.get('priority', {})
        self.priority_alpha: float = priority_cfg.get('alpha', 0.5)
        self.priority_beta: float = priority_cfg.get('beta', 0.3)
        self.priority_gamma: float = priority_cfg.get('gamma', 0.2)
        self.priority_fast_weight: float = priority_cfg.get('fast_type_weight', 1.0)
        self.priority_slow_weight: float = priority_cfg.get('slow_type_weight', 0.5)

        # ---- 扩展调度策略 (新增) ----
        # 默认仍是 FIFO, 与原 _dispatch_from_waiting_area 行为一致.
        # 运行时可通过 /api/admin/dispatch-policy 切换.
        # 0 字节修改原方法: 新入口是 dispatch_with_policy(), 与原
        # dispatch_from_waiting_area_async() 并存, 不互相干扰.
        self.dispatch_policy: str = "fifo"

    # ------------------------------------------------------------------
    #  排队号生成
    # ------------------------------------------------------------------

    def _next_queue_number(self, charge_type: str) -> str:
        if charge_type == "Fast":
            self.fast_counter += 1
            return f"F{self.fast_counter}"
        else:
            self.slow_counter += 1
            return f"T{self.slow_counter}"

    def _fault_waiting_for_type(self, charge_type: str) -> List[dict]:
        return (self.fast_fault_waiting if charge_type == "Fast"
                else self.slow_fault_waiting)

    def _has_fault_waiting(self) -> bool:
        return bool(self.fast_fault_waiting or self.slow_fault_waiting)

    # ------------------------------------------------------------------
    #  动态优先级计算
    # ------------------------------------------------------------------

    def calculate_priority(self, car: dict, current_vtime: datetime) -> float:
        """
        计算车辆动态优先级分数。
        公式: P_i = alpha*(1-SOC_i) + beta*W_type + gamma*ln(t_wait_i+1)
        - SOC_i  = current_kwh / battery_capacity_kwh  (0~1)
        - W_type = fast_type_weight (Fast) / slow_type_weight (Slow)
        - t_wait_i = 已等待分钟数
        分数越高越优先。
        """
        battery_cap = car.get("battery_capacity_kwh", self.battery_capacity)
        current_kwh = car.get("current_vehicle_kwh", 0.0)
        soc = current_kwh / battery_cap if battery_cap > 0 else 0.0
        soc = max(0.0, min(1.0, soc))

        charge_type = car.get("charge_type", "Slow")
        w_type = (self.priority_fast_weight if charge_type == "Fast"
                  else self.priority_slow_weight)

        created_at = car.get("created_at")
        if created_at and isinstance(created_at, datetime):
            wait_minutes = (current_vtime - created_at).total_seconds() / 60.0
        else:
            wait_minutes = 0.0
        wait_minutes = max(0.0, wait_minutes)

        score = (self.priority_alpha * (1.0 - soc)
                 + self.priority_beta * w_type
                 + self.priority_gamma * math.log(wait_minutes + 1.0))
        return round(score, 6)

    def _sorted_by_priority(self, waiting_list: List[dict],
                            current_vtime: datetime) -> List[dict]:
        """
        将等候区列表按优先级降序排列（分数高者先调度）。
        同分规则：等待时间长者优先 → 排队号更早者优先。
        """
        def sort_key(car):
            score = self.calculate_priority(car, current_vtime)
            created_at = car.get("created_at")
            wait_sec = (current_vtime - created_at).total_seconds() if created_at else 0.0
            qn_key = _queue_number_sort_key(car.get("queue_number", "Z999"))
            # 分数降序(-score)，等待时间降序(-wait_sec)，排队号升序
            return (-score, -wait_sec, qn_key)
        return sorted(waiting_list, key=sort_key)

    # ------------------------------------------------------------------
    #  最短完成时间调度 — 选择最优桩
    # ------------------------------------------------------------------

    # 桩选择容忍阈值(小时): 3分钟。两桩总完成时间差距在此之内时
    # 启用负载均衡 tiebreaker, 避免浮点舍入导致确定性崩塌。
    _PILE_SELECTION_TOLERANCE = 6.0 / 60.0  # 0.10 h (6 min, ~3 kWh @30kW)

    def _find_optimal_pile(self, charge_type: str,
                           requested_kwh: float) -> Optional[ChargingPile]:
        """
        在同类型非故障桩中，找到 *总完成时间最短* 的桩。
        总时间 = 该桩现有队列剩余充电时间之和 + 本车充电时间。

        二次排序 (负载均衡):
          当两桩总时间差距在容忍阈值内时, 优先选队列较短的桩,
          避免所有车挤到同一根桩上, 同时消除浮点舍入的不确定性。
        """
        candidates = [
            p for p in self.piles
            if p.type == charge_type and p.has_space
        ]
        if not candidates:
            return None

        best_pile = None
        best_time = float('inf')
        best_queue_len = float('inf')
        for pile in candidates:
            wait_time = pile.remaining_time_hours()
            own_time = requested_kwh / pile.power
            total = wait_time + own_time
            qlen = len(pile.queue)

            # 在容忍阈值外, 严格比总时间; 阈值内, 用队列长度打破平局
            if total < best_time - self._PILE_SELECTION_TOLERANCE:
                best_time = total
                best_queue_len = qlen
                best_pile = pile
            elif total <= best_time + self._PILE_SELECTION_TOLERANCE:
                # 近似平局 → 负载均衡: 队列短者优先
                if qlen < best_queue_len:
                    best_time = total
                    best_queue_len = qlen
                    best_pile = pile
        return best_pile

    # ------------------------------------------------------------------
    #  提交充电请求
    # ------------------------------------------------------------------

    async def submit_request(self, request: ChargeRequest,
                             user_id: Optional[int] = None) -> dict:
        async with self.lock:
            # 容量检查: 所有桩队列中的车 + 等候区中的车
            cars_in_piles = sum(len(p.queue) for p in self.piles)
            cars_in_waiting = (
                len(self.fast_waiting) + len(self.slow_waiting)
                + len(self.pending_requests)
                + len(self.fast_fault_waiting) + len(self.slow_fault_waiting)
            )
            total_cars = cars_in_piles + cars_in_waiting

            if total_cars >= self.waiting_capacity:
                return {"status": "rejected", "message": "系统已满，拒绝接纳",
                        "order_id": None, "queue_number": None,
                        "queue_position": None, "assigned_pile": None}

            current_vtime = self.clock.get_time()
            requested_start_time = (
                request.requested_start_time
                if request.requested_start_time is not None
                else current_vtime
            )

            queue_number = self._next_queue_number(request.charge_type)

            # 查询车辆电池容量，限制充电量不超过最大可充量
            battery_capacity = self.battery_capacity  # 配置默认值
            current_vehicle_kwh = 0.0
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Vehicle).where(
                        Vehicle.vehicle_id == request.vehicle_id)
                )
                vehicle = result.scalars().first()
                if vehicle:
                    battery_capacity = vehicle.battery_capacity_kwh
                    current_vehicle_kwh = vehicle.current_kwh

            max_chargeable = battery_capacity - current_vehicle_kwh
            if max_chargeable <= 0:
                return {"status": "rejected",
                        "message": "车辆电池已满，无需充电",
                        "order_id": None, "queue_number": None,
                        "queue_position": None, "assigned_pile": None}

            actual_requested = min(request.requested_kwh, max_chargeable)

            # 创建数据库订单
            async with AsyncSessionLocal() as session:
                new_order = ChargeOrder(
                    user_id=user_id,
                    vehicle_id=request.vehicle_id,
                    charge_type=request.charge_type,
                    requested_kwh=actual_requested,
                    queue_number=queue_number,
                    status=OrderStatus.WAITING,
                    created_at=requested_start_time,
                    requested_start_time=requested_start_time,
                )
                session.add(new_order)
                await session.commit()
                await session.refresh(new_order)
                order_id = new_order.id

            queue_item = {
                "vehicle_id": request.vehicle_id,
                "charge_type": request.charge_type,
                "requested_kwh": actual_requested,
                "charged_kwh": 0.0,
                "order_id": order_id,
                "queue_number": queue_number,
                "user_id": user_id,
                "created_at": requested_start_time,
                "requested_start_time": requested_start_time,
                "battery_capacity_kwh": battery_capacity,
                "current_vehicle_kwh": current_vehicle_kwh,
            }

            if requested_start_time > current_vtime:
                self.pending_requests.append(queue_item)
                self.pending_requests.sort(
                    key=lambda c: (c.get("requested_start_time") or c["created_at"],
                                   _queue_number_sort_key(c.get("queue_number", "Z999"))))
                return {
                    "status": "success",
                    "message": (f"已预约到虚拟时间 "
                                f"{requested_start_time.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"生效，到点后再参与调度"),
                    "order_id": order_id,
                    "queue_number": queue_number,
                    "queue_position": len(self.pending_requests),
                    "assigned_pile": None,
                }

            # 尝试直接分配到最优桩队列
            # 【FIFO 修正】只有在该类型等候区为空且无故障队列时才允许直接进桩,
            # 否则新车必须排到等候区末尾, 由 dispatch_watcher 按 FIFO 叫号.
            # 这符合详细需求"选取等候区...第一辆车进入充电区"的语义.
            same_type_waiting = (self.fast_waiting
                                 if request.charge_type == "Fast"
                                 else self.slow_waiting)
            optimal_pile = None
            if (not self._has_fault_waiting()
                    and len(same_type_waiting) == 0):
                optimal_pile = self._find_optimal_pile(
                    request.charge_type, actual_requested)

            if optimal_pile is not None:
                await self._assign_to_pile_queue(
                    optimal_pile, queue_item, current_vtime)
                position_in_queue = len(optimal_pile.queue)
                msg = f"已分配到 {optimal_pile.pile_id}"
                if actual_requested < request.requested_kwh:
                    msg += (f"（电池剩余容量限制，实际充电"
                            f"{actual_requested:.1f}度）")
                return {
                    "status": "success",
                    "message": msg,
                    "order_id": order_id,
                    "queue_number": queue_number,
                    "queue_position": position_in_queue,
                    "assigned_pile": optimal_pile.pile_id,
                }
            else:
                # 进入等候区
                waiting_list = (self.fast_waiting if request.charge_type == "Fast"
                                else self.slow_waiting)
                waiting_list.append(queue_item)
                msg = "已进入等候区排队"
                if actual_requested < request.requested_kwh:
                    msg += (f"（电池剩余容量限制，实际充电"
                            f"{actual_requested:.1f}度）")
                return {
                    "status": "success",
                    "message": msg,
                    "order_id": order_id,
                    "queue_number": queue_number,
                    "queue_position": len(waiting_list),
                    "assigned_pile": None,
                }

    # ------------------------------------------------------------------
    #  将车辆分配到桩队列
    # ------------------------------------------------------------------

    async def _assign_to_pile_queue(self, pile: ChargingPile,
                                    queue_item: dict, vtime):
        pile.queue.append(queue_item)
        is_first = len(pile.queue) == 1

        if is_first:
            old_status = pile.status
            pile.status = "CHARGING"
            await self._log_pile_status(
                pile.pile_id, old_status, "CHARGING",
                reason=f"车辆{queue_item['vehicle_id']}开始充电")

        # 更新数据库
        async with AsyncSessionLocal() as session:
            order = await session.get(ChargeOrder, queue_item['order_id'])
            if order:
                order.pile_id = pile.pile_id
                if is_first:
                    order.status = OrderStatus.CHARGING
                    order.started_at = vtime
                    order.charge_start_time = vtime
                    queue_item["charge_start_time"] = vtime  # 内存保留，供实时计费
                else:
                    order.status = OrderStatus.QUEUING
                await session.commit()

    # ------------------------------------------------------------------
    #  后台电量模拟（每 0.2 真实秒 tick, 提高时间精度）
    # ------------------------------------------------------------------

    async def simulate_battery_growth(self):
        """后台任务：按虚拟时钟推进充电 kWh，充满后结算。
        双重停止条件：1) charged_kwh >= requested_kwh
                     2) 当前总电量达到电池最大容量

        关键设计:
          - tick 粒度 0.2s, 高 ratio 下也保证 ~0.2*ratio 虚拟分钟的电量精度.
          - 增量按"实际虚拟时间差"计算, 不是 (tick * ratio), 避免 tick
            被锁/DB 拖慢时累积漂移导致车辆延迟完成.
          - cancel/stop/fault 触发时在 _finish_charging 内再按虚拟时间
            精确补算 actual_kwh, 双重保险.
        """
        last_tick_vtime = None
        while True:
            await asyncio.sleep(0.2)
            async with self.lock:
                current_vtime = self.clock.get_time()
                if not self.clock.running:
                    last_tick_vtime = current_vtime
                    continue
                if last_tick_vtime is None:
                    last_tick_vtime = current_vtime
                    continue

                # 实际经过的虚拟分钟数(自适应, 防止 tick 漂移)
                elapsed_min = max(0.0,
                    (current_vtime - last_tick_vtime).total_seconds() / 60.0)
                last_tick_vtime = current_vtime

                for pile in self.piles:
                    if pile.status != "CHARGING" or len(pile.queue) == 0:
                        continue

                    car = pile.queue[0]
                    # kWh 增量 = power(kW) * 经过的虚拟分钟 / 60(分/小时)
                    increment = pile.power * elapsed_min / 60.0
                    car["charged_kwh"] += increment
                    car["charged_kwh"] = round(car["charged_kwh"], 4)

                    # 电池容量上限检查
                    battery_cap = car.get("battery_capacity_kwh",
                                          self.battery_capacity)
                    current_base = car.get("current_vehicle_kwh", 0.0)
                    max_chargeable = battery_cap - current_base

                    # 双重停止条件
                    should_stop = (car["charged_kwh"] >= car["requested_kwh"]
                                   or car["charged_kwh"] >= max_chargeable)

                    if should_stop:
                        car["charged_kwh"] = min(
                            car["charged_kwh"],
                            car["requested_kwh"],
                            max_chargeable)
                        reason = "充满"
                        if car["charged_kwh"] >= max_chargeable:
                            reason = "电池达到最大容量"
                        print(f"[Clock {current_vtime.strftime('%H:%M:%S')}] "
                              f"车辆 {car['vehicle_id']} 于 {pile.pile_id} "
                              f"{reason} {car['charged_kwh']:.2f}度 出场。")

                        await self._finish_charging(
                            pile, current_vtime,
                            status=OrderStatus.COMPLETED)
                        # 【关键】充电完成后禁止自动调度，不从等候区拉车

    # ------------------------------------------------------------------
    #  充电完成处理（计费 + 队列推进 + 统计）
    # ------------------------------------------------------------------

    async def _finish_charging(self, pile: ChargingPile, current_vtime,
                               status: str = OrderStatus.COMPLETED
                               ) -> Optional[dict]:
        if len(pile.queue) == 0:
            return None

        # 弹出 position 0 的车
        finished_car = pile.queue.pop(0)
        bill_data = None

        # 【精确补算】tick 粒度可能让 charged_kwh 比真实值低. 在结算前
        # 按 charge_start_time 到 current_vtime 的精确虚拟时间重算 kWh.
        # 对 cancel / stop / fault 触发场景尤其重要.
        charge_start_in_mem = finished_car.get("charge_start_time")
        if charge_start_in_mem is not None:
            elapsed_hours = max(0.0,
                (current_vtime - charge_start_in_mem).total_seconds() / 3600.0)
            precise_kwh = elapsed_hours * pile.power
            battery_cap = finished_car.get("battery_capacity_kwh",
                                           self.battery_capacity)
            current_base = finished_car.get("current_vehicle_kwh", 0.0)
            max_chargeable = max(0.0, battery_cap - current_base)
            # 精确值与 tick 累计值取较大者(防止 tick 已经四舍五入到稍高),
            # 但不超过请求电量和电池容量上限.
            tick_kwh = finished_car.get("charged_kwh", 0.0)
            actual_kwh = min(max(precise_kwh, tick_kwh),
                             finished_car["requested_kwh"],
                             max_chargeable)
        else:
            actual_kwh = min(finished_car.get("charged_kwh", 0.0),
                             finished_car["requested_kwh"])
        actual_kwh = round(actual_kwh, 4)

        async with AsyncSessionLocal() as session:
            order = await session.get(ChargeOrder, finished_car['order_id'])
            if order:
                charge_start = order.charge_start_time or order.started_at
                fee_result = calculate_fee(
                    charge_start, current_vtime, actual_kwh)

                order.status = status
                order.finished_at = current_vtime
                order.charge_end_time = current_vtime
                order.charged_kwh = actual_kwh
                order.bill_code = _generate_bill_code()
                order.total_power = fee_result["total_power"]
                order.charge_duration = fee_result["duration_hours"]
                order.power_fee = fee_result["power_fee"]
                order.service_fee = fee_result["service_fee"]
                order.total_fee = fee_result["total_fee"]

                # 生成账单记录
                bill = Bill(
                    bill_code=order.bill_code,
                    order_id=order.id,
                    vehicle_id=order.vehicle_id,
                    pile_id=pile.pile_id,
                    charge_type=order.charge_type,
                    charge_start_time=charge_start,
                    charge_end_time=current_vtime,
                    charge_duration=fee_result["duration_hours"],
                    total_power=fee_result["total_power"],
                    power_fee=fee_result["power_fee"],
                    service_fee=fee_result["service_fee"],
                    total_fee=fee_result["total_fee"],
                    created_at=current_vtime,
                )
                session.add(bill)
                await session.flush()  # 获取 bill.id

                # 生成详单记录（每个连续时段段一条）
                detail = fee_result.get("detail", {})
                for seg in detail.get("segments", []):
                    bd = BillDetail(
                        bill_id=bill.id,
                        period=seg.get("period", ""),
                        start_time=seg.get("start", ""),
                        end_time=seg.get("end", ""),
                        duration_minutes=seg.get("minutes", 0),
                        kwh=seg.get("kwh", 0.0),
                        rate=seg.get("rate", 0.0),
                        fee=seg.get("fee", 0.0),
                    )
                    session.add(bd)

                await session.commit()
                bill_data = fee_result

            # 充电完成后，更新车辆当前电量
            vehicle_result = await session.execute(
                select(Vehicle).where(
                    Vehicle.vehicle_id == finished_car['vehicle_id'])
            )
            vehicle = vehicle_result.scalars().first()
            if vehicle:
                vehicle.current_kwh = min(
                    vehicle.current_kwh + actual_kwh,
                    vehicle.battery_capacity_kwh)
                await session.commit()

        # 更新桩统计
        if bill_data:
            pile.total_charge_count += 1
            pile.total_charge_duration += bill_data["duration_hours"]
            pile.total_charge_amount += bill_data["total_power"]
            pile.total_power_fee += bill_data["power_fee"]
            pile.total_service_fee += bill_data["service_fee"]
            pile.total_total_fee += bill_data["total_fee"]

        # 队列推进：如果队列中还有车，新的 queue[0] 自动开始充电
        if len(pile.queue) > 0:
            next_car = pile.queue[0]
            next_car["charged_kwh"] = 0.0
            next_car["charge_start_time"] = current_vtime  # 内存保留，供实时计费
            async with AsyncSessionLocal() as session:
                next_order = await session.get(
                    ChargeOrder, next_car['order_id'])
                if next_order:
                    next_order.status = OrderStatus.CHARGING
                    next_order.started_at = current_vtime
                    next_order.charge_start_time = current_vtime
                    await session.commit()
            # pile 保持 CHARGING 状态
        else:
            old_status = pile.status
            pile.status = "IDLE"
            await self._log_pile_status(
                pile.pile_id, old_status, "IDLE",
                reason=f"车辆{finished_car['vehicle_id']}充电完成，队列为空")

        return bill_data

    # ------------------------------------------------------------------
    #  等候区调度 → 桩队列 (dispatch_watcher 调用)
    # ------------------------------------------------------------------

    def _dispatch_from_waiting_area(self):
        """
        将等候区的车辆按 FIFO(提交时间)顺序分配到最优桩队列.
        仅由 dispatch_watcher 后台任务或管理员手动触发.

        策略(严格按详细需求):
          1) numbering_paused 或有故障队列时直接返回(故障期间暂停叫号).
          2) 对快/慢两类等候区, 各自按 FIFO 顺序遍历, 每辆车找最优桩.
          3) 找不到合适桩的车保留在等候区, 继续尝试下一辆.
        """
        if self.numbering_paused or self._has_fault_waiting():
            return

        current_vtime = self.clock.get_time()

        for charge_type, waiting_list in [
            ("Fast", self.fast_waiting),
            ("Slow", self.slow_waiting),
        ]:
            if not waiting_list:
                continue
            # 【调试日志】打印等候区 FIFO 顺序快照(用 ASCII 字符避免 GBK 崩溃)
            queue_repr = " -> ".join(
                f"{c['vehicle_id']}({c.get('queue_number','?')})"
                for c in waiting_list)
            print(f"[Dispatch {current_vtime.strftime('%H:%M:%S')}] "
                  f"{charge_type} 等候区 FIFO 顺序: {queue_repr}")

            # 严格按 FIFO 顺序遍历(不排序)
            sorted_cars = list(waiting_list)
            dispatched_order_ids = []
            dispatched_updates = []

            for car in sorted_cars:
                optimal = self._find_optimal_pile(
                    charge_type, car["requested_kwh"])
                if optimal is None:
                    print(f"[Dispatch {current_vtime.strftime('%H:%M:%S')}]"
                          f"   - {car['vehicle_id']}"
                          f"({car.get('queue_number','?')}) "
                          f"{car['requested_kwh']:.1f}kWh: 暂无可用桩")
                    continue
                print(f"[Dispatch {current_vtime.strftime('%H:%M:%S')}]"
                      f"   [OK] {car['vehicle_id']}"
                      f"({car.get('queue_number','?')}) "
                      f"{car['requested_kwh']:.1f}kWh -> {optimal.pile_id}"
                      f" (等待 {optimal.remaining_time_hours():.2f}h)")
                dispatched_order_ids.append(car["order_id"])
                optimal.queue.append(car)
                is_first = len(optimal.queue) == 1
                old_status = optimal.status
                if is_first:
                    optimal.status = "CHARGING"
                dispatched_updates.append(
                    (optimal, car, is_first, old_status))

            # 从等候区移除已调度的车辆
            if dispatched_order_ids:
                dispatched_set = set(dispatched_order_ids)
                waiting_list[:] = [
                    c for c in waiting_list
                    if c["order_id"] not in dispatched_set
                ]
                self._pending_db_updates.extend(dispatched_updates)

    async def _dispatch_fault_waiting_async(self):
        self.numbering_paused = self._has_fault_waiting()
        current_vtime = self.clock.get_time()

        for charge_type in ["Fast", "Slow"]:
            fault_queue = self._fault_waiting_for_type(charge_type)
            if not fault_queue:
                continue

            remaining = []
            for car in list(fault_queue):
                optimal = self._find_optimal_pile(
                    charge_type, car["requested_kwh"])
                if optimal is None:
                    remaining.append(car)
                    continue
                car["charged_kwh"] = 0.0
                await self._assign_to_pile_queue(
                    optimal, car, current_vtime)

            fault_queue[:] = remaining

        self.numbering_paused = self._has_fault_waiting()

    async def dispatch_from_waiting_area_async(self):
        """异步版本：调度 + DB 更新"""
        await self._activate_pending_requests()
        await self._dispatch_fault_waiting_async()
        if self._has_fault_waiting():
            return

        self._pending_db_updates: list = []
        self._dispatch_from_waiting_area()

        current_vtime = self.clock.get_time()
        for pile, car, is_first, old_status in self._pending_db_updates:
            async with AsyncSessionLocal() as session:
                order = await session.get(ChargeOrder, car['order_id'])
                if order:
                    order.pile_id = pile.pile_id
                    if is_first:
                        order.status = OrderStatus.CHARGING
                        order.started_at = current_vtime
                        order.charge_start_time = current_vtime
                        car["charge_start_time"] = current_vtime  # 内存保留，供实时计费
                    else:
                        order.status = OrderStatus.QUEUING
                    await session.commit()
            if is_first:
                await self._log_pile_status(
                    pile.pile_id, old_status, "CHARGING",
                    reason=f"等候区车辆{car['vehicle_id']}调度充电")
        self._pending_db_updates = []

    async def _activate_pending_requests(self):
        """把已到预约生效时间的请求移入普通等候区，再参与调度。"""
        if not self.pending_requests:
            return

        current_vtime = self.clock.get_time()
        ready = [
            item for item in self.pending_requests
            if (item.get("requested_start_time") or item["created_at"]) <= current_vtime
        ]
        if not ready:
            return

        ready_ids = {item["order_id"] for item in ready}
        self.pending_requests[:] = [
            item for item in self.pending_requests
            if item["order_id"] not in ready_ids
        ]

        ready.sort(key=lambda c: (
            c.get("requested_start_time") or c["created_at"],
            _queue_number_sort_key(c.get("queue_number", "Z999"))))
        for item in ready:
            target = self.fast_waiting if item["charge_type"] == "Fast" else self.slow_waiting
            target.append(item)

    async def dispatch_watcher(self):
        """后台任务：每 2 秒检查桩队列空位，从等候区调度"""
        while True:
            await asyncio.sleep(2.0)
            async with self.lock:
                await self.dispatch_from_waiting_area_async()

    # ------------------------------------------------------------------
    #  扩展调度入口 (新增 — 0 字节修改 dispatch_watcher 本身)
    # ------------------------------------------------------------------

    async def dispatch_with_policy(self, policy_name: str = "fifo") -> dict:
        """
        按指定策略执行一次调度. 走与 FIFO 不同的代码路径, 不影响
        dispatch_watcher 默认每 2 秒的 FIFO 循环.

        流程:
          1. 激活预约到期的 pending_requests
          2. 故障队列优先调度 (与 FIFO 路径相同)
          3. 用策略对象 compute 一组 Assignment
          4. _apply_assignments 落库 + 移出等候区
        """
        from src.core.policies import get_policy, available_policies

        if policy_name not in available_policies():
            return {"status": "failed",
                    "message": f"未知策略 {policy_name}, "
                               f"可选: {available_policies()}"}

        async with self.lock:
            await self._activate_pending_requests()
            await self._dispatch_fault_waiting_async()
            if self._has_fault_waiting():
                return {"status": "skipped",
                        "reason": "故障队列优先, 本轮不分配"}

            policy = get_policy(policy_name)
            assignments = policy.assign(
                self.piles,
                self.fast_waiting + self.slow_waiting,
                self.clock.get_time())

            if not assignments:
                return {"status": "success",
                        "policy": policy_name,
                        "assignments_count": 0,
                        "message": "无可用桩位或无等候车辆"}

            count_by_pile = await self._apply_assignments(assignments)
            return {
                "status": "success",
                "policy": policy_name,
                "assignments_count": len(assignments),
                "count_by_pile": count_by_pile,
                "message": f"已分配 {len(assignments)} 辆车",
            }

    async def _apply_assignments(self, assignments) -> dict:
        """
        把策略产出的 Assignment 列表落到内存+DB+日志.
        完全复用 _assign_to_pile_queue 写库, 不重复实现.
        返回 {pile_id: count} 统计.
        """
        from collections import Counter
        # 先去重 (按 order_id), 防止策略 bug 给出重复分配
        seen_order = set()
        unique_assignments = []
        for a in assignments:
            oid = a.car.get("order_id")
            if oid in seen_order:
                continue
            seen_order.add(oid)
            unique_assignments.append(a)

        # 把车从等候区移除 (在内存里)
        order_to_car = {a.car.get("order_id"): a for a in unique_assignments}
        self.fast_waiting[:] = [
            c for c in self.fast_waiting
            if c.get("order_id") not in order_to_car]
        self.slow_waiting[:] = [
            c for c in self.slow_waiting
            if c.get("order_id") not in order_to_car]

        # 落库 (复用现有方法, 不重写)
        current_vtime = self.clock.get_time()
        for a in unique_assignments:
            await self._assign_to_pile_queue(
                a.pile_obj, a.car, current_vtime)

        return dict(Counter(a.pile_id for a in unique_assignments))

    # ------------------------------------------------------------------
    #  取消请求（等候区 + 充电区均可）
    # ------------------------------------------------------------------

    async def cancel_request(self, order_id: int) -> dict:
        async with self.lock:
            current_vtime = self.clock.get_time()

            # 1. 检查等候区
            for waiting_list in [
                self.pending_requests,
                self.fast_waiting, self.slow_waiting,
                self.fast_fault_waiting, self.slow_fault_waiting,
            ]:
                for i, item in enumerate(waiting_list):
                    if item['order_id'] == order_id:
                        waiting_list.pop(i)
                        await self._update_order_status(
                            order_id, OrderStatus.CANCELLED)
                        return {"status": "success",
                                "message": "已取消等候区排队，不产生费用"}

            # 2. 检查桩队列
            for pile in self.piles:
                for i, item in enumerate(pile.queue):
                    if item['order_id'] == order_id:
                        if i == 0:
                            # 正在充电，停止计费出账单
                            bill = await self._finish_charging(
                                pile, current_vtime,
                                status=OrderStatus.CANCELLED)
                            return {
                                "status": "success",
                                "message": "已取消充电并生成账单",
                            }
                        else:
                            # 在桩队列中等候，直接移除
                            pile.queue.pop(i)
                            await self._update_order_status(
                                order_id, OrderStatus.CANCELLED)
                            return {"status": "success",
                                    "message": "已取消桩队列排队，不产生费用"}

            # 3. 检查数据库
            async with AsyncSessionLocal() as session:
                order = await session.get(ChargeOrder, order_id)
                if order is None:
                    return {"status": "failed", "message": "订单不存在"}
                if order.status in (OrderStatus.COMPLETED,
                                    OrderStatus.CANCELLED):
                    return {"status": "failed",
                            "message": f"订单已处于 {order.status} 状态"}

            return {"status": "failed", "message": "订单不在任何队列中"}

    # ------------------------------------------------------------------
    #  主动停止充电（仅 position 0 正在充电的车）
    # ------------------------------------------------------------------

    async def stop_charging(self, order_id: int) -> dict:
        async with self.lock:
            current_vtime = self.clock.get_time()

            for pile in self.piles:
                if (len(pile.queue) > 0
                        and pile.queue[0]['order_id'] == order_id
                        and pile.status == "CHARGING"):
                    print(f"[Clock {current_vtime.strftime('%H:%M:%S')}] "
                          f"车辆 {pile.queue[0]['vehicle_id']} 于 "
                          f"{pile.pile_id} 主动中断充电。")

                    bill = await self._finish_charging(
                        pile, current_vtime,
                        status=OrderStatus.COMPLETED)
                    # 【关键】充电完成后禁止自动调度

                    return {
                        "status": "success",
                        "message": "已中断充电并生成账单",
                        "total_power": bill['total_power'] if bill else 0.0,
                        "power_fee": bill['power_fee'] if bill else 0.0,
                        "service_fee": bill['service_fee'] if bill else 0.0,
                        "total_fee": bill['total_fee'] if bill else 0.0,
                    }

            async with AsyncSessionLocal() as session:
                order = await session.get(ChargeOrder, order_id)
                if order is None:
                    return {"status": "failed", "message": "订单不存在"}
                if order.status == OrderStatus.WAITING:
                    return {"status": "failed",
                            "message": "该订单在等候区，请使用取消接口"}
                if order.status == OrderStatus.QUEUING:
                    return {"status": "failed",
                            "message": "该订单在桩队列排队中，尚未充电"}

            return {"status": "failed", "message": "该订单不在充电中"}

    # ------------------------------------------------------------------
    #  修改充电请求（仅限等候区 WAITING 状态）
    # ------------------------------------------------------------------

    async def modify_request(self, order_id: int,
                             new_charge_type: Optional[str] = None,
                             new_requested_kwh: Optional[float] = None
                             ) -> dict:
        async with self.lock:
            # 在等候区中查找
            found_list = None
            found_idx = None
            found_item = None

            for wlist, ctype in [(self.fast_waiting, "Fast"),
                                 (self.slow_waiting, "Slow")]:
                for i, item in enumerate(wlist):
                    if item['order_id'] == order_id:
                        found_list = wlist
                        found_idx = i
                        found_item = item
                        break
                if found_item:
                    break

            if found_item is None:
                # 检查是否在桩队列或充电中
                for pile in self.piles:
                    for item in pile.queue:
                        if item['order_id'] == order_id:
                            return {"status": "failed",
                                    "message": "车辆已在充电区，"
                                               "不允许修改，请先取消"}
                return {"status": "failed",
                        "message": "未找到该订单或订单不在等候区"}

            new_qn = found_item['queue_number']

            # 修改充电模式
            if new_charge_type and new_charge_type != found_item['charge_type']:
                found_list.pop(found_idx)
                found_item['charge_type'] = new_charge_type
                new_qn = self._next_queue_number(new_charge_type)
                found_item['queue_number'] = new_qn

                target_list = (self.fast_waiting if new_charge_type == "Fast"
                               else self.slow_waiting)
                target_list.append(found_item)

            # 修改充电量
            if new_requested_kwh is not None and new_requested_kwh > 0:
                found_item['requested_kwh'] = new_requested_kwh

            # 更新数据库
            async with AsyncSessionLocal() as session:
                order = await session.get(ChargeOrder, order_id)
                if order:
                    if new_charge_type:
                        order.charge_type = new_charge_type
                    order.queue_number = new_qn
                    if new_requested_kwh is not None:
                        order.requested_kwh = new_requested_kwh
                    await session.commit()

            return {
                "status": "success",
                "message": "修改成功",
                "new_queue_number": new_qn,
            }

    # ------------------------------------------------------------------
    #  故障处理
    # ------------------------------------------------------------------

    async def fault_pile(self, pile_id: str,
                         duration_minutes: Optional[float] = None) -> dict:
        """将充电桩置为故障状态，处理正在充电和排队的车辆"""
        async with self.lock:
            pile = self._get_pile(pile_id)
            if pile is None:
                return {"status": "failed", "message": "充电桩不存在"}
            if pile.status == "FAULT":
                return {"status": "failed", "message": "充电桩已处于故障状态"}

            current_vtime = self.clock.get_time()
            displaced_cars = []

            # 正在充电的车（position 0）→ 停止计费，生成账单
            if len(pile.queue) > 0 and pile.status == "CHARGING":
                await self._finish_charging(
                    pile, current_vtime, status=OrderStatus.FAULTED)

            # 收集剩余排队车辆
            displaced_cars = list(pile.queue)
            pile.queue.clear()
            old_status = pile.status
            pile.status = "FAULT"
            await self._log_pile_status(
                pile.pile_id, old_status, "FAULT",
                reason="管理员设置故障", operator="admin")

            # 优先级调度：将故障队列车辆优先分配到同类型其他桩
            fault_queue = self._fault_waiting_for_type(pile.type)
            displaced_cars.sort(key=lambda c: _queue_number_sort_key(
                c.get('queue_number', 'Z999')))
            fault_queue.extend(displaced_cars)
            self.numbering_paused = self._has_fault_waiting()

            if displaced_cars:
                await self._dispatch_fault_waiting_async()

            if duration_minutes is not None and duration_minutes > 0:
                self._schedule_auto_recover(pile_id, float(duration_minutes))

            return {"status": "success",
                    "message": f"充电桩 {pile_id} 已置为故障，"
                               f"已调度 {len(displaced_cars)} 辆车",
                    "pile_id": pile_id}

    def _schedule_auto_recover(self, pile_id: str, duration_minutes: float):
        old_task = self.fault_recover_tasks.pop(pile_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self.fault_recover_tasks[pile_id] = asyncio.create_task(
            self._auto_recover_after(pile_id, duration_minutes)
        )

    async def _auto_recover_after(self, pile_id: str, duration_minutes: float):
        real_seconds = max(
            0.0, duration_minutes * 60.0 / max(self.clock.ratio, 1e-9))
        try:
            await asyncio.sleep(real_seconds)
            await self.recover_pile(pile_id)
        finally:
            self.fault_recover_tasks.pop(pile_id, None)

    async def _priority_dispatch(self, displaced_cars: List[dict],
                                 charge_type: str, current_vtime):
        """优先级调度：暂停叫号，将故障车辆优先分配到同类型桩"""
        self.numbering_paused = True

        # 按排队号排序
        displaced_cars.sort(key=lambda c: _queue_number_sort_key(
            c.get('queue_number', 'Z999')))

        remaining = []
        for car in displaced_cars:
            optimal = self._find_optimal_pile(
                charge_type, car["requested_kwh"])
            if optimal:
                car["charged_kwh"] = 0.0
                await self._assign_to_pile_queue(
                    optimal, car, current_vtime)
            else:
                remaining.append(car)

        # 分配不了的放回等候区前端（优先）
        if remaining:
            waiting_list = (self.fast_waiting if charge_type == "Fast"
                            else self.slow_waiting)
            for car in reversed(remaining):
                # 更新数据库状态回 WAITING
                await self._update_order_status(
                    car['order_id'], OrderStatus.WAITING)
                waiting_list.insert(0, car)

        self.numbering_paused = False

    async def recover_pile(self, pile_id: str) -> dict:
        async with self.lock:
            pile = self._get_pile(pile_id)
            if pile is None:
                return {"status": "failed", "message": "charging pile not found"}
            if pile.status != "FAULT":
                return {"status": "failed", "message": "charging pile is not faulted"}

            pile.status = "IDLE"
            task = self.fault_recover_tasks.pop(pile_id, None)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
            await self._log_pile_status(
                pile.pile_id, "FAULT", "IDLE",
                reason="admin recovered fault", operator="admin")
            current_vtime = self.clock.get_time()

            if self._has_fault_waiting():
                await self._dispatch_fault_waiting_async()
                if self._has_fault_waiting():
                    return {"status": "success",
                            "message": f"pile {pile_id} recovered",
                            "pile_id": pile_id}

            same_type_piles = [
                p for p in self.piles
                if p.type == pile.type and p.pile_id != pile_id
            ]
            if any(len(p.queue) > 1 for p in same_type_piles):
                await self._time_order_dispatch(pile.type, current_vtime)

            return {"status": "success",
                    "message": f"pile {pile_id} recovered",
                    "pile_id": pile_id}

    async def _time_order_dispatch(self, charge_type: str,
                                   current_vtime):
        """
        时间顺序调度：将同类型桩中未充电的车辆 + 等候区车辆
        合并后按排队号顺序重新分配。
        """
        self.numbering_paused = True

        # 收集所有未充电的车辆（位于 position 1+ 的）
        collected = []
        for pile in self.piles:
            if pile.type != charge_type or pile.status == "FAULT":
                continue
            # 保留 position 0（正在充电的），移除 position 1+
            while len(pile.queue) > 1:
                car = pile.queue.pop()
                collected.append(car)

        # 加入等候区车辆
        waiting_list = (self.fast_waiting if charge_type == "Fast"
                        else self.slow_waiting)
        collected.extend(waiting_list)
        waiting_list.clear()

        # 按排队号排序
        collected.sort(key=lambda c: _queue_number_sort_key(
            c.get('queue_number', 'Z999')))

        # 重新分配
        remaining = []
        for car in collected:
            car["charged_kwh"] = 0.0
            optimal = self._find_optimal_pile(
                charge_type, car["requested_kwh"])
            if optimal:
                await self._assign_to_pile_queue(
                    optimal, car, current_vtime)
            else:
                remaining.append(car)

        # 分配不了的放回等候区
        waiting_list.extend(remaining)
        for car in remaining:
            await self._update_order_status(
                car['order_id'], OrderStatus.WAITING)

        self.numbering_paused = False

    # ------------------------------------------------------------------
    #  启停桩
    # ------------------------------------------------------------------

    async def start_pile(self, pile_id: str) -> dict:
        async with self.lock:
            pile = self._get_pile(pile_id)
            if pile is None:
                return {"status": "failed", "message": "充电桩不存在"}
            if pile.status == "FAULT":
                return await self._recover_pile_internal(pile)
            if pile.status != "IDLE" and len(pile.queue) == 0:
                old_status = pile.status
                pile.status = "IDLE"
                await self._log_pile_status(
                    pile.pile_id, old_status, "IDLE",
                    reason="管理员启动桩", operator="admin")
            return {"status": "success",
                    "message": f"充电桩 {pile_id} 已启动",
                    "pile_id": pile_id}

    async def _recover_pile_internal(self, pile: ChargingPile) -> dict:
        old_status = pile.status
        pile.status = "IDLE"
        await self._log_pile_status(
            pile.pile_id, old_status, "IDLE",
            reason="管理员启动恢复故障桩", operator="admin")
        current_vtime = self.clock.get_time()
        same_type_piles = [
            p for p in self.piles
            if p.type == pile.type and p.pile_id != pile.pile_id
        ]
        if any(len(p.queue) > 1 for p in same_type_piles):
            await self._time_order_dispatch(pile.type, current_vtime)
        return {"status": "success",
                "message": f"充电桩 {pile.pile_id} 已从故障恢复",
                "pile_id": pile.pile_id}

    async def stop_pile(self, pile_id: str) -> dict:
        """关闭充电桩（等同于故障处理）"""
        return await self.fault_pile(pile_id)

    async def update_power(self, fast_power: float, slow_power: float) -> dict:
        """更新充电功率配置"""
        async with self.lock:
            self.fast_power = float(fast_power)
            self.slow_power = float(slow_power)
            for pile in self.piles:
                if pile.type == "Fast":
                    pile.power = self.fast_power
                elif pile.type == "Slow":
                    pile.power = self.slow_power
            return {"status": "success", "message": f"功率已更新为 快充: {self.fast_power}kW, 慢充: {self.slow_power}kW"}

    async def update_system_params(self, params: dict) -> dict:
        """
        运行时更新系统调度参数。
        支持字段：waiting_area_size, pile_queue_length,
                  alpha, beta, gamma, fast_type_weight, slow_type_weight
        """
        async with self.lock:
            changed = []

            if "waiting_area_size" in params:
                n = int(params["waiting_area_size"])
                current_total = (sum(len(p.queue) for p in self.piles)
                                 + len(self.fast_waiting)
                                 + len(self.slow_waiting))
                if n < current_total:
                    return {"status": "failed",
                            "message": f"等候区容量 {n} 小于当前在队车辆数 {current_total}，拒绝修改"}
                if n < 1:
                    return {"status": "failed", "message": "等候区容量必须 ≥ 1"}
                self.waiting_capacity = n
                changed.append(f"等候区容量 → {n}")

            if "pile_queue_length" in params:
                m = int(params["pile_queue_length"])
                max_current = max((len(p.queue) for p in self.piles), default=0)
                if m < max_current:
                    return {"status": "failed",
                            "message": f"桩队列长度 {m} 小于当前最长队列 {max_current}，拒绝修改"}
                if m < 1:
                    return {"status": "failed", "message": "桩队列长度必须 ≥ 1"}
                self.pile_queue_length = m
                for pile in self.piles:
                    pile.max_queue_length = m
                changed.append(f"桩队列长度 → {m}")

            if "alpha" in params:
                v = float(params["alpha"])
                if v < 0:
                    return {"status": "failed", "message": "alpha 必须 ≥ 0"}
                self.priority_alpha = v
                changed.append(f"alpha → {v}")

            if "beta" in params:
                v = float(params["beta"])
                if v < 0:
                    return {"status": "failed", "message": "beta 必须 ≥ 0"}
                self.priority_beta = v
                changed.append(f"beta → {v}")

            if "gamma" in params:
                v = float(params["gamma"])
                if v < 0:
                    return {"status": "failed", "message": "gamma 必须 ≥ 0"}
                self.priority_gamma = v
                changed.append(f"gamma → {v}")

            if "fast_type_weight" in params:
                v = float(params["fast_type_weight"])
                if v < 0:
                    return {"status": "failed", "message": "fast_type_weight 必须 ≥ 0"}
                self.priority_fast_weight = v
                changed.append(f"fast_type_weight → {v}")

            if "slow_type_weight" in params:
                v = float(params["slow_type_weight"])
                if v < 0:
                    return {"status": "failed", "message": "slow_type_weight 必须 ≥ 0"}
                self.priority_slow_weight = v
                changed.append(f"slow_type_weight → {v}")

            if not changed:
                return {"status": "failed", "message": "未提供任何有效参数"}

            return {
                "status": "success",
                "changed": changed,
                "current": {
                    "waiting_area_size": self.waiting_capacity,
                    "pile_queue_length": self.pile_queue_length,
                    "alpha": self.priority_alpha,
                    "beta": self.priority_beta,
                    "gamma": self.priority_gamma,
                    "fast_type_weight": self.priority_fast_weight,
                    "slow_type_weight": self.priority_slow_weight,
                }
            }

    # ------------------------------------------------------------------
    #  服务重启恢复
    # ------------------------------------------------------------------

    async def restore_from_db(self):
        """
        从数据库恢复未完成的订单到内存队列。
        - CHARGING 状态订单恢复到对应桩 position 0
        - QUEUING  状态订单恢复到对应桩后续位置
        - WAITING  状态订单恢复到等候区
        恢复后按 started_at / created_at 排序，保证位置正确。
        """
        from src.models.models import Vehicle as _Vehicle
        async with AsyncSessionLocal() as session:
            # 查所有未完成订单
            stmt = (select(ChargeOrder)
                    .where(ChargeOrder.status.in_([
                        OrderStatus.CHARGING,
                        OrderStatus.QUEUING,
                        OrderStatus.WAITING,
                    ]))
                    .order_by(ChargeOrder.started_at.nulls_last(),
                              ChargeOrder.created_at))
            result = await session.execute(stmt)
            orders = result.scalars().all()

            if not orders:
                print("[Restore] 无未完成订单，跳过恢复。")
                return

            # 预取车辆信息
            vehicle_ids = list({o.vehicle_id for o in orders})
            veh_result = await session.execute(
                select(_Vehicle).where(_Vehicle.vehicle_id.in_(vehicle_ids)))
            vehicles = {v.vehicle_id: v for v in veh_result.scalars().all()}

        print(f"[Restore] 发现 {len(orders)} 笔未完成订单，开始恢复...")

        # 先恢复 CHARGING / QUEUING（按 pile_id 分组）
        pile_orders: dict[str, list] = {}
        waiting_orders = []
        for o in orders:
            if o.status in (OrderStatus.CHARGING, OrderStatus.QUEUING):
                if o.pile_id:
                    pile_orders.setdefault(o.pile_id, []).append(o)
            else:
                waiting_orders.append(o)

        for pile_id, pile_order_list in pile_orders.items():
            pile = self._get_pile(pile_id)
            if pile is None:
                print(f"[Restore] 警告: 桩 {pile_id} 不存在，跳过 {len(pile_order_list)} 笔订单")
                continue

            # CHARGING 排前面，QUEUING 按 created_at 排序
            charging = [o for o in pile_order_list if o.status == OrderStatus.CHARGING]
            queuing = sorted(
                [o for o in pile_order_list if o.status == OrderStatus.QUEUING],
                key=lambda o: o.created_at or datetime.min)
            sorted_orders = charging + queuing

            for o in sorted_orders:
                veh = vehicles.get(o.vehicle_id)
                queue_item = {
                    "vehicle_id": o.vehicle_id,
                    "charge_type": o.charge_type,
                    "requested_kwh": o.requested_kwh or 0.0,
                    "charged_kwh": o.charged_kwh or 0.0,
                    "order_id": o.id,
                    "queue_number": o.queue_number or "",
                    "user_id": o.user_id,
                    "created_at": o.created_at,
                    "battery_capacity_kwh": veh.battery_capacity_kwh if veh else self.battery_capacity,
                    "current_vehicle_kwh": veh.current_kwh if veh else 0.0,
                    "charge_start_time": o.charge_start_time,
                }
                pile.queue.append(queue_item)

            if pile.queue:
                pile.status = "CHARGING"
                print(f"[Restore] 桩 {pile_id} 恢复 {len(pile.queue)} 辆车")

        current_vtime = self.clock.get_time()

        # 恢复预约区 / 等候区
        for o in waiting_orders:
            veh = vehicles.get(o.vehicle_id)
            requested_start = o.requested_start_time or o.created_at
            queue_item = {
                "vehicle_id": o.vehicle_id,
                "charge_type": o.charge_type,
                "requested_kwh": o.requested_kwh or 0.0,
                "charged_kwh": 0.0,
                "order_id": o.id,
                "queue_number": o.queue_number or "",
                "user_id": o.user_id,
                "created_at": requested_start,
                "requested_start_time": requested_start,
                "battery_capacity_kwh": veh.battery_capacity_kwh if veh else self.battery_capacity,
                "current_vehicle_kwh": veh.current_kwh if veh else 0.0,
            }
            if requested_start and requested_start > current_vtime:
                self.pending_requests.append(queue_item)
            elif o.charge_type == "Fast":
                self.fast_waiting.append(queue_item)
            else:
                self.slow_waiting.append(queue_item)

        print(f"[Restore] 等候区恢复: Fast={len(self.fast_waiting)}, Slow={len(self.slow_waiting)}, Pending={len(self.pending_requests)}")

        # 同步排队号计数器，避免重复
        all_qns = [o.queue_number for o in orders if o.queue_number]
        for qn in all_qns:
            if qn.startswith("F"):
                try:
                    num = int(qn[1:])
                    self.fast_counter = max(self.fast_counter, num)
                except ValueError:
                    pass
            elif qn.startswith("T"):
                try:
                    num = int(qn[1:])
                    self.slow_counter = max(self.slow_counter, num)
                except ValueError:
                    pass

        print(f"[Restore] 排队号计数器: Fast={self.fast_counter}, Slow={self.slow_counter}")

    # ------------------------------------------------------------------
    #  查询方法
    # ------------------------------------------------------------------

    def _get_pile(self, pile_id: str) -> Optional[ChargingPile]:
        for p in self.piles:
            if p.pile_id == pile_id:
                return p
        return None

    def get_system_status(self) -> dict:
        current_vtime = self.clock.get_time()
        piles_data = []
        for p in self.piles:
            queue_items = []
            for i, car in enumerate(p.queue):
                current_fee = 0.0
                current_power_fee = 0.0
                current_service_fee = 0.0
                charge_start_str = None

                if i == 0 and p.status == "CHARGING":
                    charge_start = car.get("charge_start_time")
                    charged_kwh = car.get("charged_kwh", 0.0)
                    if charge_start and charged_kwh > 0:
                        fee_r = calculate_fee(
                            charge_start, current_vtime, charged_kwh)
                        current_fee = fee_r["total_fee"]
                        current_power_fee = fee_r["power_fee"]
                        current_service_fee = fee_r["service_fee"]
                    if charge_start:
                        charge_start_str = charge_start.strftime(
                            "%Y-%m-%d %H:%M:%S")

                queue_items.append({
                    "position": i,
                    "order_id": car["order_id"],
                    "vehicle_id": car["vehicle_id"],
                    "charge_type": car.get("charge_type", p.type),
                    "queue_number": car.get("queue_number"),
                    "requested_kwh": car["requested_kwh"],
                    "charged_kwh": car.get("charged_kwh", 0.0),
                    "current_fee": current_fee,
                    "current_power_fee": current_power_fee,
                    "current_service_fee": current_service_fee,
                    "charge_start_time": charge_start_str,
                    "user_id": car.get("user_id"),
                    "wait_duration_minutes": None,
                    "priority_score": round(
                        self.calculate_priority(car, current_vtime), 4),
                    "soc": round(
                        car.get("current_vehicle_kwh", 0.0)
                        / max(car.get("battery_capacity_kwh",
                                      self.battery_capacity), 1e-9), 4),
                    "wait_minutes": round(
                        (current_vtime - car["created_at"]).total_seconds() / 60.0
                        if car.get("created_at") else 0.0, 2),
                })
            piles_data.append({
                "pile_id": p.pile_id,
                "type": p.type,
                "status": p.status,
                "power": p.power,
                "queue_len": len(p.queue),
                "max_queue_len": p.max_queue_length,
                "total_charge_count": p.total_charge_count,
                "total_charge_duration": round(p.total_charge_duration, 4),
                "total_charge_amount": round(p.total_charge_amount, 4),
                "total_power_fee": round(p.total_power_fee, 2),
                "total_service_fee": round(p.total_service_fee, 2),
                "total_total_fee": round(p.total_total_fee, 2),
                "queue_items": queue_items,
            })
        return {
            "piles": piles_data,
            "fast_waiting_count": len(self.fast_waiting),
            "slow_waiting_count": len(self.slow_waiting),
            "pending_count": len(self.pending_requests),
        }

    def get_queue_position(self, order_id: int) -> Optional[dict]:
        """查询某订单的排队位置"""
        for i, item in enumerate(self.pending_requests):
            if item['order_id'] == order_id:
                return {
                    "order_id": order_id,
                    "queue_number": item.get("queue_number"),
                    "status": "SCHEDULED",
                    "ahead_count": i,
                    "pile_id": None,
                    "charge_type": item["charge_type"],
                    "requested_kwh": item["requested_kwh"],
                }

        # 检查等候区
        for ctype, wlist in [("Fast", self.fast_waiting),
                             ("Slow", self.slow_waiting)]:
            for i, item in enumerate(wlist):
                if item['order_id'] == order_id:
                    return {
                        "order_id": order_id,
                        "queue_number": item.get("queue_number"),
                        "status": "WAITING",
                        "ahead_count": i,
                        "pile_id": None,
                        "charge_type": ctype,
                        "requested_kwh": item["requested_kwh"],
                    }

        # 检查桩队列
        for pile in self.piles:
            for i, item in enumerate(pile.queue):
                if item['order_id'] == order_id:
                    return {
                        "order_id": order_id,
                        "queue_number": item.get("queue_number"),
                        "status": "CHARGING" if i == 0 else "QUEUING",
                        "ahead_count": i,
                        "pile_id": pile.pile_id,
                        "charge_type": pile.type,
                        "requested_kwh": item["requested_kwh"],
                    }

        return None

    def get_waiting_area(self) -> dict:
        """获取等候区车辆信息（按优先级降序排列）"""
        current_vtime = self.clock.get_time()

        def _build_item(item, charge_type):
            battery_cap = item.get("battery_capacity_kwh", self.battery_capacity)
            current_kwh = item.get("current_vehicle_kwh", 0.0)
            soc = current_kwh / max(battery_cap, 1e-9)
            created_at = item.get("created_at")
            wait_min = (
                (current_vtime - created_at).total_seconds() / 60.0
                if created_at else 0.0
            )
            return {
                "order_id": item["order_id"],
                "vehicle_id": item["vehicle_id"],
                "queue_number": item.get("queue_number", ""),
                "charge_type": charge_type,
                "requested_kwh": item["requested_kwh"],
                "user_id": item.get("user_id"),
                "waiting_since": created_at,
                "requested_start_time": item.get("requested_start_time"),
                "priority_score": round(
                    self.calculate_priority(item, current_vtime), 4),
                "soc": round(max(0.0, min(1.0, soc)), 4),
                "wait_minutes": round(max(0.0, wait_min), 2),
                "battery_capacity_kwh": battery_cap,
                "current_vehicle_kwh": current_kwh,
            }

        # 按优先级降序输出（仅展示，不修改内部 waiting_list）
        sorted_fast = self._sorted_by_priority(self.fast_waiting, current_vtime)
        sorted_slow = self._sorted_by_priority(self.slow_waiting, current_vtime)

        return {
            "pending": [_build_item(i, i["charge_type"])
                        for i in self.pending_requests],
            "fast_waiting": [_build_item(i, "Fast") for i in sorted_fast],
            "slow_waiting": [_build_item(i, "Slow") for i in sorted_slow],
        }

    # ------------------------------------------------------------------
    #  内部工具
    # ------------------------------------------------------------------

    async def _update_order_status(self, order_id: int, status: str):
        async with AsyncSessionLocal() as session:
            order = await session.get(ChargeOrder, order_id)
            if order:
                order.status = status
                await session.commit()

    async def _log_pile_status(self, pile_id: str, old_status: str,
                               new_status: str, reason: str = "",
                               operator: str = "system"):
        """记录充电桩状态变更到数据库日志表"""
        if old_status == new_status:
            return
        async with AsyncSessionLocal() as session:
            log = PileStatusLog(
                pile_id=pile_id,
                old_status=old_status,
                new_status=new_status,
                reason=reason,
                operator=operator,
                changed_at=self.clock.get_time(),
            )
            session.add(log)
            await session.commit()
