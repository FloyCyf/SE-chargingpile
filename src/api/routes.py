from fastapi import APIRouter, Request, HTTPException
from src.api.schemas import (
    ChargeRequest, ChargeResponse, SystemStatusResponse,
    BillResponse, CancelResponse, StopResponse, FeeDetailItem,
)
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder
from src.core.billing import BillingEngine

router = APIRouter()


@router.post("/requests/", response_model=ChargeResponse)
async def submit_charge_request(req_body: ChargeRequest, request: Request):
    """提交充电请求: 分配桩资源或进入等待队列"""
    scheduler = request.app.state.scheduler
    result = await scheduler.submit_request(req_body)
    return ChargeResponse(**result)


@router.get("/system/dump", response_model=SystemStatusResponse)
async def get_site_status(request: Request):
    """(调试探测用) 获取充电站实时状态和队列长度"""
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    return SystemStatusResponse(**status)


@router.get("/bills/{order_id}", response_model=BillResponse)
async def get_bill(order_id: int, request: Request):
    """查询指定订单的账单详情"""
    async with AsyncSessionLocal() as session:
        order = await session.get(ChargeOrder, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")

    # 对已结算的订单，重新计算分时明细用于展示
    fee_detail = None
    if (order.started_at is not None and order.finished_at is not None
            and order.charge_kwh is not None and order.charge_kwh > 0):
        billing: BillingEngine = request.app.state.scheduler.billing
        fee_result = billing.calculate_fee(
            order.started_at, order.finished_at, order.charge_kwh)
        fee_detail = [FeeDetailItem(**d) for d in fee_result['detail']]

    return BillResponse(
        order_id=order.id,
        vehicle_id=order.vehicle_id,
        pile_id=order.pile_id,
        charge_type=order.charge_type,
        status=order.status,
        start_soc=order.start_soc,
        end_soc=order.end_soc,
        target_soc=order.target_soc,
        charge_kwh=order.charge_kwh,
        electricity_fee=order.electricity_fee,
        service_fee=order.service_fee,
        timeout_fee=order.timeout_fee,
        total_fee=order.total_fee,
        created_at=order.created_at,
        started_at=order.started_at,
        finished_at=order.finished_at,
        left_at=order.left_at,
        fee_detail=fee_detail,
    )


@router.post("/requests/{order_id}/cancel", response_model=CancelResponse)
async def cancel_request(order_id: int, request: Request):
    """取消排队中的充电请求（仅 QUEUING 状态可操作）"""
    scheduler = request.app.state.scheduler
    result = await scheduler.cancel_request(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return CancelResponse(**result)


@router.post("/requests/{order_id}/stop", response_model=StopResponse)
async def stop_charging(order_id: int, request: Request):
    """主动停止充电，按已充入电量即时结算"""
    scheduler = request.app.state.scheduler
    result = await scheduler.stop_charging(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return StopResponse(**result)
