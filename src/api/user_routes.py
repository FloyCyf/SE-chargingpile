from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy import select
from src.api.auth import get_current_user
from src.api.schemas import (
    ChargeRequest, ChargeResponse, BillResponse, CancelResponse,
    StopResponse, FeeDetail, ModifyRequest, ModifyResponse,
    QueuePositionResponse, OrderListResponse, OrderSummary,
    VehicleCreate, VehicleUpdate, VehicleResponse, VehicleListResponse,
)
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder, Vehicle
from src.core.billing import calculate_fee

router = APIRouter()


@router.post("/requests/", response_model=ChargeResponse)
async def submit_charge_request(
    req_body: ChargeRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """提交充电请求（需认证）"""
    scheduler = request.app.state.scheduler
    result = await scheduler.submit_request(
        req_body, user_id=current_user["user_id"])
    return ChargeResponse(**result)


@router.get("/queue-position/{order_id}",
            response_model=QueuePositionResponse)
async def get_queue_position(
    order_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """查看本车排队号码和前方等待数"""
    scheduler = request.app.state.scheduler
    result = scheduler.get_queue_position(order_id)
    if result is None:
        # 从数据库查已完成的订单
        async with AsyncSessionLocal() as session:
            order = await session.get(ChargeOrder, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="订单不存在")
            return QueuePositionResponse(
                order_id=order.id,
                queue_number=order.queue_number,
                status=order.status,
                ahead_count=0,
                pile_id=order.pile_id,
                charge_type=order.charge_type,
                requested_kwh=order.requested_kwh or 0.0,
            )
    return QueuePositionResponse(**result)


@router.post("/requests/{order_id}/modify", response_model=ModifyResponse)
async def modify_request(
    order_id: int,
    body: ModifyRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """修改充电请求（仅限等候区）"""
    scheduler = request.app.state.scheduler
    result = await scheduler.modify_request(
        order_id,
        new_charge_type=body.charge_type,
        new_requested_kwh=body.requested_kwh,
    )
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return ModifyResponse(**result)


@router.post("/requests/{order_id}/cancel", response_model=CancelResponse)
async def cancel_request(
    order_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """取消充电请求"""
    scheduler = request.app.state.scheduler
    result = await scheduler.cancel_request(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return CancelResponse(**result)


@router.post("/requests/{order_id}/stop", response_model=StopResponse)
async def stop_charging(
    order_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """主动停止充电"""
    scheduler = request.app.state.scheduler
    result = await scheduler.stop_charging(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return StopResponse(**result)


@router.get("/bills/{order_id}", response_model=BillResponse)
async def get_bill(
    order_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """查看充电详单"""
    async with AsyncSessionLocal() as session:
        order = await session.get(ChargeOrder, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")

    detail = None
    if (order.charge_start_time is not None
            and order.charge_end_time is not None
            and order.total_power is not None and order.total_power > 0):
        fee_result = calculate_fee(
            order.charge_start_time, order.charge_end_time, order.total_power)
        detail = FeeDetail(**fee_result['detail'])

    return BillResponse(
        order_id=order.id,
        vehicle_id=order.vehicle_id,
        pile_id=order.pile_id,
        charge_type=order.charge_type,
        status=order.status,
        requested_kwh=order.requested_kwh or 0.0,
        queue_number=order.queue_number,
        bill_code=order.bill_code,
        charge_start_time=order.charge_start_time,
        charge_end_time=order.charge_end_time,
        charge_duration=order.charge_duration,
        total_power=order.total_power,
        power_fee=order.power_fee,
        service_fee=order.service_fee,
        total_fee=order.total_fee,
        created_at=order.created_at,
        started_at=order.started_at,
        finished_at=order.finished_at,
        detail=detail,
    )


@router.get("/orders", response_model=OrderListResponse)
async def list_orders(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """查看当前用户所有订单"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChargeOrder)
            .where(ChargeOrder.user_id == current_user["user_id"])
            .order_by(ChargeOrder.created_at.desc())
        )
        orders = result.scalars().all()

    items = []
    for o in orders:
        items.append(OrderSummary(
            order_id=o.id,
            vehicle_id=o.vehicle_id,
            charge_type=o.charge_type,
            requested_kwh=o.requested_kwh or 0.0,
            queue_number=o.queue_number,
            status=o.status,
            pile_id=o.pile_id,
            total_fee=o.total_fee,
            created_at=o.created_at,
            finished_at=o.finished_at,
        ))
    return OrderListResponse(orders=items)


# ------------------------------------------------------------------
#  车辆管理
# ------------------------------------------------------------------

@router.post("/vehicles", response_model=VehicleResponse)
async def register_vehicle(
    body: VehicleCreate,
    current_user: dict = Depends(get_current_user),
):
    """注册车辆（绑定电池最大容量）"""
    if body.current_kwh > body.battery_capacity_kwh:
        raise HTTPException(
            status_code=400, detail="当前电量不能超过电池最大容量")

    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(Vehicle).where(Vehicle.vehicle_id == body.vehicle_id)
        )
        if existing.scalars().first() is not None:
            raise HTTPException(status_code=400, detail="该车牌号已注册")

        vehicle = Vehicle(
            vehicle_id=body.vehicle_id,
            battery_capacity_kwh=body.battery_capacity_kwh,
            current_kwh=body.current_kwh,
            owner_id=current_user["user_id"],
        )
        session.add(vehicle)
        await session.commit()
        await session.refresh(vehicle)

    return VehicleResponse(
        vehicle_id=vehicle.vehicle_id,
        battery_capacity_kwh=vehicle.battery_capacity_kwh,
        current_kwh=vehicle.current_kwh,
        owner_id=vehicle.owner_id,
    )


@router.get("/vehicles", response_model=VehicleListResponse)
async def list_vehicles(
    current_user: dict = Depends(get_current_user),
):
    """查询当前用户的所有车辆"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Vehicle).where(
                Vehicle.owner_id == current_user["user_id"])
        )
        vehicles = result.scalars().all()

    return VehicleListResponse(vehicles=[
        VehicleResponse(
            vehicle_id=v.vehicle_id,
            battery_capacity_kwh=v.battery_capacity_kwh,
            current_kwh=v.current_kwh,
            owner_id=v.owner_id,
        ) for v in vehicles
    ])


@router.get("/vehicles/{vehicle_id}", response_model=VehicleResponse)
async def get_vehicle(vehicle_id: str):
    """查询指定车辆信息"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Vehicle).where(Vehicle.vehicle_id == vehicle_id)
        )
        vehicle = result.scalars().first()

    if vehicle is None:
        raise HTTPException(status_code=404, detail="车辆不存在")

    return VehicleResponse(
        vehicle_id=vehicle.vehicle_id,
        battery_capacity_kwh=vehicle.battery_capacity_kwh,
        current_kwh=vehicle.current_kwh,
        owner_id=vehicle.owner_id,
    )


@router.put("/vehicles/{vehicle_id}", response_model=VehicleResponse)
async def update_vehicle(
    vehicle_id: str,
    body: VehicleUpdate,
    current_user: dict = Depends(get_current_user),
):
    """更新车辆信息（电池容量/当前电量）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Vehicle).where(Vehicle.vehicle_id == vehicle_id)
        )
        vehicle = result.scalars().first()
        if vehicle is None:
            raise HTTPException(status_code=404, detail="车辆不存在")

        if body.battery_capacity_kwh is not None:
            vehicle.battery_capacity_kwh = body.battery_capacity_kwh
        if body.current_kwh is not None:
            cap = body.battery_capacity_kwh or vehicle.battery_capacity_kwh
            if body.current_kwh > cap:
                raise HTTPException(
                    status_code=400, detail="当前电量不能超过电池最大容量")
            vehicle.current_kwh = body.current_kwh

        await session.commit()
        await session.refresh(vehicle)

    return VehicleResponse(
        vehicle_id=vehicle.vehicle_id,
        battery_capacity_kwh=vehicle.battery_capacity_kwh,
        current_kwh=vehicle.current_kwh,
        owner_id=vehicle.owner_id,
    )
