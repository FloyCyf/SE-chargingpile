from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from sqlalchemy import select, func, and_
from src.api.auth import require_admin
from src.api.schemas import (
    PileControlRequest, PileControlResponse,
    SystemStatusResponse, WaitingAreaResponse,
    ReportResponse, ReportItem,
)
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder, OrderStatus

router = APIRouter()


@router.post("/piles/{pile_id}/control", response_model=PileControlResponse)
async def control_pile(
    pile_id: str,
    body: PileControlRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """启动/关闭/故障 充电桩"""
    scheduler = request.app.state.scheduler
    action = body.action.lower()

    if action == "start":
        result = await scheduler.start_pile(pile_id)
    elif action == "stop":
        result = await scheduler.stop_pile(pile_id)
    elif action == "fault":
        result = await scheduler.fault_pile(pile_id)
    elif action == "recover":
        result = await scheduler.recover_pile(pile_id)
    else:
        raise HTTPException(status_code=400,
                            detail="无效操作，可选: start/stop/fault/recover")

    if result['status'] == 'failed':
        raise HTTPException(status_code=400, detail=result['message'])
    return PileControlResponse(**result)


@router.get("/piles", response_model=SystemStatusResponse)
async def get_all_piles(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """获取所有充电桩状态（含队列详情）"""
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    return SystemStatusResponse(**status)


@router.get("/waiting-area", response_model=WaitingAreaResponse)
async def get_waiting_area(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """获取等候区车辆信息"""
    scheduler = request.app.state.scheduler
    data = scheduler.get_waiting_area()
    return WaitingAreaResponse(**data)


@router.post("/dispatch")
async def manual_dispatch(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """手动触发等候区 → 桩队列调度"""
    scheduler = request.app.state.scheduler
    async with scheduler.lock:
        await scheduler.dispatch_from_waiting_area_async()
    return {"status": "success", "message": "调度完成"}


@router.get("/reports", response_model=ReportResponse)
async def get_reports(
    request: Request,
    period: str = Query("day", description="统计周期: day/week/month"),
    date: str = Query(None, description="日期 YYYY-MM-DD，默认今天"),
    admin: dict = Depends(require_admin),
):
    """生成充电桩报表"""
    # 解析日期范围
    if date:
        try:
            base_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式错误，应为 YYYY-MM-DD")
    else:
        base_date = datetime.now()

    if period == "day":
        start_dt = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1)
    elif period == "week":
        # 本周一 ~ 下周一
        weekday = base_date.weekday()
        start_dt = (base_date - timedelta(days=weekday)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=7)
    elif period == "month":
        start_dt = base_date.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_dt.month == 12:
            end_dt = start_dt.replace(year=start_dt.year + 1, month=1)
        else:
            end_dt = start_dt.replace(month=start_dt.month + 1)
    else:
        raise HTTPException(status_code=400,
                            detail="无效周期，可选: day/week/month")

    # 从数据库聚合已完成订单
    async with AsyncSessionLocal() as session:
        stmt = (
            select(
                ChargeOrder.pile_id,
                ChargeOrder.charge_type,
                func.count(ChargeOrder.id).label("charge_count"),
                func.coalesce(func.sum(ChargeOrder.charge_duration), 0.0).label("total_duration"),
                func.coalesce(func.sum(ChargeOrder.total_power), 0.0).label("total_kwh"),
                func.coalesce(func.sum(ChargeOrder.power_fee), 0.0).label("total_power_fee"),
                func.coalesce(func.sum(ChargeOrder.service_fee), 0.0).label("total_service_fee"),
                func.coalesce(func.sum(ChargeOrder.total_fee), 0.0).label("total_total_fee"),
            )
            .where(and_(
                ChargeOrder.status.in_([
                    OrderStatus.COMPLETED, OrderStatus.FAULTED]),
                ChargeOrder.finished_at >= start_dt,
                ChargeOrder.finished_at < end_dt,
                ChargeOrder.pile_id.isnot(None),
            ))
            .group_by(ChargeOrder.pile_id, ChargeOrder.charge_type)
            .order_by(ChargeOrder.pile_id)
        )
        result = await session.execute(stmt)
        rows = result.all()

    items = []
    for row in rows:
        items.append(ReportItem(
            pile_id=row.pile_id or "",
            pile_type=row.charge_type or "",
            charge_count=row.charge_count,
            total_duration=round(row.total_duration, 4),
            total_kwh=round(row.total_kwh, 4),
            total_power_fee=round(row.total_power_fee, 2),
            total_service_fee=round(row.total_service_fee, 2),
            total_total_fee=round(row.total_total_fee, 2),
        ))

    return ReportResponse(
        period=period,
        start_date=start_dt.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        items=items,
    )
