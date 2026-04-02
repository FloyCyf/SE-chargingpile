import asyncio
from typing import Dict, List, Optional
from src.api.schemas import ChargeRequest
from src.core.clock import VirtualClock
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder

class ChargingPile:
    def __init__(self, pile_id: str, pile_type: str):
        self.pile_id = pile_id
        self.type = pile_type # 'Fast' or 'Slow'
        self.status = "IDLE" # IDLE, CHARGING
        self.vehicle_id: Optional[str] = None
        self.current_soc: Optional[float] = None
        self.target_soc: Optional[float] = None
        self.db_order_id: Optional[int] = None
    
    def assign_vehicle(self, vehicle_id: str, current_soc: float, target_soc: float, order_id: int):
        self.status = "CHARGING"
        self.vehicle_id = vehicle_id
        self.current_soc = current_soc
        self.target_soc = target_soc
        self.db_order_id = order_id
    
    def free_pile(self):
        self.status = "IDLE"
        self.vehicle_id = None
        self.current_soc = None
        self.target_soc = None
        self.db_order_id = None

class FIFOScheduler:
    def __init__(self, config: dict):
        self.config = config
        self.waiting_capacity = config['system']['waiting_area_capacity']
        self.fast_rate = config['simulation'].get('fast_charge_percent_per_min', 0.01)
        self.slow_rate = config['simulation'].get('slow_charge_percent_per_min', 0.005)
        
        # 初始化时钟单例
        self.clock = VirtualClock(config)
        self.lock = asyncio.Lock()
        
        self.piles: List[ChargingPile] = []
        fast_count = config['system']['fast_charging_piles']
        slow_count = config['system']['slow_charging_piles']
        
        for i in range(fast_count):
            self.piles.append(ChargingPile(f"F{i+1}", "Fast"))
        for i in range(slow_count):
            self.piles.append(ChargingPile(f"S{i+1}", "Slow"))
            
        # Queues 存包含 order_id 的实体字典
        self.fast_queue: List[dict] = []
        self.slow_queue: List[dict] = []

    async def submit_request(self, request: ChargeRequest) -> dict:
        async with self.lock:
            current_charging = sum(1 for p in self.piles if p.status == "CHARGING")
            total_waiting = len(self.fast_queue) + len(self.slow_queue)
            
            if current_charging + total_waiting >= self.waiting_capacity:
                return {"status": "rejected", "message": "等候区已满，拒绝接纳", "queue_position": None, "assigned_pile": None}
            
            # 使用自建的时钟打表
            current_vtime = self.clock.get_time()
            
            # 首先写入数据库订单记录(QUEUING)
            async with AsyncSessionLocal() as session:
                new_order = ChargeOrder(
                    vehicle_id=request.vehicle_id,
                    charge_type=request.charge_type,
                    start_soc=request.current_soc,
                    target_soc=request.target_soc,
                    status="QUEUING",
                    created_at=current_vtime
                )
                session.add(new_order)
                await session.commit()
                await session.refresh(new_order)
                order_id = new_order.id
                
            queue_item = {
                "vehicle_id": request.vehicle_id,
                "charge_type": request.charge_type,
                "current_soc": request.current_soc,
                "target_soc": request.target_soc,
                "order_id": order_id
            }
            
            # Try to find empty pile immediately
            empty_piles = [p for p in self.piles if p.type == request.charge_type and p.status == "IDLE"]
            if empty_piles:
                pile = empty_piles[0]
                await self._assign_to_pile(pile, queue_item, current_vtime)
                return {"status": "success", "message": f"分配到{request.charge_type}资源", "assigned_pile": pile.pile_id, "queue_position": None}
            else:
                # Enqueue
                if request.charge_type == "Fast":
                    self.fast_queue.append(queue_item)
                    pos = len(self.fast_queue)
                else:
                    self.slow_queue.append(queue_item)
                    pos = len(self.slow_queue)
                    
                return {"status": "success", "message": "已进入排队", "queue_position": pos, "assigned_pile": None}

    async def _assign_to_pile(self, pile: ChargingPile, queue_item: dict, assign_time):
        """内部绑定桩与更新数据库态"""
        pile.assign_vehicle(
            vehicle_id=queue_item['vehicle_id'], 
            current_soc=queue_item['current_soc'], 
            target_soc=queue_item['target_soc'], 
            order_id=queue_item['order_id']
        )
        async with AsyncSessionLocal() as session:
            order = await session.get(ChargeOrder, queue_item['order_id'])
            if order:
                order.pile_id = pile.pile_id
                order.status = "CHARGING"
                order.started_at = assign_time
                await session.commit()

    async def dispatch_from_queues(self, current_vtime):
        """试图将队列中的车辆调度到空闲的桩"""
        for pile in self.piles:
            if pile.status == "IDLE":
                if pile.type == "Fast" and self.fast_queue:
                    req = self.fast_queue.pop(0)
                    await self._assign_to_pile(pile, req, current_vtime)
                elif pile.type == "Slow" and self.slow_queue:
                    req = self.slow_queue.pop(0)
                    await self._assign_to_pile(pile, req, current_vtime)

    def get_system_status(self) -> dict:
        return {
            "piles": [
                {
                    "pile_id": p.pile_id,
                    "type": p.type,
                    "status": p.status,
                    "vehicle_id": p.vehicle_id,
                    "current_soc": p.current_soc,
                    "target_soc": p.target_soc
                } for p in self.piles
            ],
            "fast_queue_count": len(self.fast_queue),
            "slow_queue_count": len(self.slow_queue)
        }

    async def simulate_battery_growth(self):
        """后台任务：结合虚拟时钟加速推进电量，并落盘结束数据"""
        while True:
            await asyncio.sleep(1.0) # 真实的1秒走一周期
            async with self.lock:
                current_vtime = self.clock.get_time()
                for pile in self.piles:
                    if pile.status == "CHARGING" and pile.current_soc is not None:
                        # 对于每个周期（默认在时钟里配的为虚拟1分钟），电量增长该倍率的值
                        rate = self.fast_rate if pile.type == "Fast" else self.slow_rate
                        pile.current_soc += rate
                        pile.current_soc = round(pile.current_soc, 4)
                        
                        if pile.current_soc >= pile.target_soc:
                            print(f"[Clock {current_vtime.strftime('%H:%M:%S')}] 车辆 {pile.vehicle_id} 于 {pile.pile_id} 充满出场。")
                            
                            # 更新数据库结束状态
                            async with AsyncSessionLocal() as session:
                                order = await session.get(ChargeOrder, pile.db_order_id)
                                if order:
                                    order.status = "COMPLETED"
                                    # 使用虚拟时间打发票
                                    order.finished_at = current_vtime 
                                    order.end_soc = pile.current_soc
                                    await session.commit()
                                    
                            pile.free_pile()
                
                # 每次有车走空出位置时检查队列看能否接人
                await self.dispatch_from_queues(current_vtime)
