from fastapi import APIRouter, Request, HTTPException
from src.api.schemas import (
    ChargeRequest, ChargeResponse, SystemStatusResponse,
    BillResponse, CancelResponse, StopResponse, FeeDetail,
)
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder
from src.core.billing import calculate_fee

router = APIRouter()


@router.post("/requests/", response_model=ChargeResponse)
async def submit_charge_request(req_body: ChargeRequest, request: Request):
    """提交充电请求（无需认证的兼容接口）"""
    scheduler = request.app.state.scheduler
    result = await scheduler.submit_request(req_body)
    return ChargeResponse(**result)


@router.get("/system/dump", response_model=SystemStatusResponse)
async def get_site_status(request: Request):
    """获取充电站实时状态"""
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


@router.post("/requests/{order_id}/cancel", response_model=CancelResponse)
async def cancel_request(order_id: int, request: Request):
    """取消充电请求"""
    scheduler = request.app.state.scheduler
    result = await scheduler.cancel_request(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return CancelResponse(**result)


@router.post("/requests/{order_id}/stop", response_model=StopResponse)
async def stop_charging(order_id: int, request: Request):
    """主动停止充电"""
    scheduler = request.app.state.scheduler
    result = await scheduler.stop_charging(order_id)
    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return StopResponse(**result)
