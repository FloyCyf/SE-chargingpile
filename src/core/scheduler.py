import asyncio
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
        self.pile_queue_length = config['system'].get('pile_queue_length', 2)
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

        # 排队号计数器
        self.fast_counter: int = 0
        self.slow_counter: int = 0

        # 故障调度锁
        self.numbering_paused: bool = False

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

    # ------------------------------------------------------------------
    #  最短完成时间调度 — 选择最优桩
    # ------------------------------------------------------------------

    def _find_optimal_pile(self, charge_type: str,
                           requested_kwh: float) -> Optional[ChargingPile]:
        """
        在同类型非故障桩中，找到 *总完成时间最短* 的桩。
        总时间 = 该桩现有队列剩余充电时间之和 + 本车充电时间
        """
        candidates = [
            p for p in self.piles
            if p.type == charge_type and p.has_space
        ]
        if not candidates:
            return None

        best_pile = None
        best_time = float('inf')
        for pile in candidates:
            wait_time = pile.remaining_time_hours()
            own_time = requested_kwh / pile.power
            total = wait_time + own_time
            if total < best_time:
                best_time = total
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
            cars_in_waiting = len(self.fast_waiting) + len(self.slow_waiting)
            total_cars = cars_in_piles + cars_in_waiting

            if total_cars >= self.waiting_capacity:
                return {"status": "rejected", "message": "系统已满，拒绝接纳",
                        "order_id": None, "queue_number": None,
                        "queue_position": None, "assigned_pile": None}

            current_vtime = self.clock.get_time()
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
                    created_at=current_vtime,
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
                "created_at": current_vtime,
                "battery_capacity_kwh": battery_capacity,
                "current_vehicle_kwh": current_vehicle_kwh,
            }

            # 尝试直接分配到最优桩队列
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
                else:
                    order.status = OrderStatus.QUEUING
                await session.commit()

    # ------------------------------------------------------------------
    #  后台电量模拟（每秒 tick）
    # ------------------------------------------------------------------

    async def simulate_battery_growth(self):
        """后台任务：按虚拟时钟推进充电 kWh，充满后结算。
        双重停止条件：1) charged_kwh >= requested_kwh
                     2) 当前总电量达到电池最大容量
        """
        while True:
            await asyncio.sleep(1.0)
            async with self.lock:
                current_vtime = self.clock.get_time()

                for pile in self.piles:
                    if pile.status != "CHARGING" or len(pile.queue) == 0:
                        continue

                    car = pile.queue[0]  # position 0 = 正在充电
                    # 每真实秒对应 virtual_ratio 虚拟分钟
                    # kWh 增量 = power(kW) * 虚拟分钟数 / 60
                    increment = pile.power * self.virtual_ratio / 60.0
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
        actual_kwh = min(finished_car["charged_kwh"],
                         finished_car["requested_kwh"])

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
        将等候区的车辆按 FIFO 顺序分配到最优桩队列。
        仅由 dispatch_watcher 后台任务或管理员手动触发。
        """
        if self.numbering_paused:
            return

        for charge_type, waiting_list in [
            ("Fast", self.fast_waiting),
            ("Slow", self.slow_waiting),
        ]:
            dispatched_indices = []
            for i, car in enumerate(waiting_list):
                optimal = self._find_optimal_pile(
                    charge_type, car["requested_kwh"])
                if optimal is None:
                    break  # 没有空位了
                dispatched_indices.append((i, car, optimal))

            # 逆序移除以避免索引偏移
            for i, car, pile in reversed(dispatched_indices):
                waiting_list.pop(i)
                pile.queue.append(car)
                is_first = len(pile.queue) == 1
                old_status = pile.status
                if is_first:
                    pile.status = "CHARGING"
                # 数据库更新在异步方法中完成
                self._pending_db_updates.append(
                    (pile, car, is_first, old_status))

    async def dispatch_from_waiting_area_async(self):
        """异步版本：调度 + DB 更新"""
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
                    else:
                        order.status = OrderStatus.QUEUING
                    await session.commit()
            if is_first:
                await self._log_pile_status(
                    pile.pile_id, old_status, "CHARGING",
                    reason=f"等候区车辆{car['vehicle_id']}调度充电")
        self._pending_db_updates = []

    async def dispatch_watcher(self):
        """后台任务：每 2 秒检查桩队列空位，从等候区调度"""
        while True:
            await asyncio.sleep(2.0)
            async with self.lock:
                await self.dispatch_from_waiting_area_async()

    # ------------------------------------------------------------------
    #  取消请求（等候区 + 充电区均可）
    # ------------------------------------------------------------------

    async def cancel_request(self, order_id: int) -> dict:
        async with self.lock:
            current_vtime = self.clock.get_time()

            # 1. 检查等候区
            for waiting_list in [self.fast_waiting, self.slow_waiting]:
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

    async def fault_pile(self, pile_id: str) -> dict:
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
            if displaced_cars:
                await self._priority_dispatch(
                    displaced_cars, pile.type, current_vtime)

            return {"status": "success",
                    "message": f"充电桩 {pile_id} 已置为故障，"
                               f"已调度 {len(displaced_cars)} 辆车",
                    "pile_id": pile_id}

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
        """充电桩故障恢复"""
        async with self.lock:
            pile = self._get_pile(pile_id)
            if pile is None:
                return {"status": "failed", "message": "充电桩不存在"}
            if pile.status != "FAULT":
                return {"status": "failed", "message": "充电桩不处于故障状态"}

            pile.status = "IDLE"
            await self._log_pile_status(
                pile.pile_id, "FAULT", "IDLE",
                reason="管理员恢复故障", operator="admin")
            current_vtime = self.clock.get_time()

            # 如果同类型桩仍有排队车辆，进行时间顺序重新调度
            same_type_piles = [
                p for p in self.piles
                if p.type == pile.type and p.pile_id != pile_id
            ]
            has_queued = any(
                len(p.queue) > 1 for p in same_type_piles)

            if has_queued:
                await self._time_order_dispatch(
                    pile.type, current_vtime)

            return {"status": "success",
                    "message": f"充电桩 {pile_id} 已恢复",
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

    # ------------------------------------------------------------------
    #  查询方法
    # ------------------------------------------------------------------

    def _get_pile(self, pile_id: str) -> Optional[ChargingPile]:
        for p in self.piles:
            if p.pile_id == pile_id:
                return p
        return None

    def get_system_status(self) -> dict:
        piles_data = []
        for p in self.piles:
            queue_items = []
            for i, car in enumerate(p.queue):
                queue_items.append({
                    "position": i,
                    "order_id": car["order_id"],
                    "vehicle_id": car["vehicle_id"],
                    "queue_number": car.get("queue_number"),
                    "requested_kwh": car["requested_kwh"],
                    "charged_kwh": car.get("charged_kwh", 0.0),
                    "user_id": car.get("user_id"),
                    "wait_duration_minutes": None,
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
        }

    def get_queue_position(self, order_id: int) -> Optional[dict]:
        """查询某订单的排队位置"""
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
        """获取等候区车辆信息"""
        fast_list = []
        for item in self.fast_waiting:
            fast_list.append({
                "order_id": item["order_id"],
                "vehicle_id": item["vehicle_id"],
                "queue_number": item.get("queue_number", ""),
                "charge_type": "Fast",
                "requested_kwh": item["requested_kwh"],
                "user_id": item.get("user_id"),
                "waiting_since": item.get("created_at"),
            })
        slow_list = []
        for item in self.slow_waiting:
            slow_list.append({
                "order_id": item["order_id"],
                "vehicle_id": item["vehicle_id"],
                "queue_number": item.get("queue_number", ""),
                "charge_type": "Slow",
                "requested_kwh": item["requested_kwh"],
                "user_id": item.get("user_id"),
                "waiting_since": item.get("created_at"),
            })
        return {"fast_waiting": fast_list, "slow_waiting": slow_list}

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
