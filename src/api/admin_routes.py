from datetime import datetime, timedelta
import csv
import io
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import select, func, and_
from sqlalchemy.orm import selectinload
from src.api.auth import require_admin
from src.api.schemas import (
    PileControlRequest, PileControlResponse,
    SystemStatusResponse, WaitingAreaResponse,
    ReportResponse, ReportItem,
    PileStatusLogItem, PileStatusLogResponse,
    BillItem, BillDetailItem, BillListResponse,
    AdminOrderItem, AdminOrderListResponse,
)
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder, OrderStatus, PileStatusLog, Bill, BillDetail
from src.core.billing import get_billing_config, update_billing_config

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


@router.get("/pile-status-logs", response_model=PileStatusLogResponse)
async def get_pile_status_logs(
    request: Request,
    pile_id: str = Query(None, description="充电桩编号，为空则查询全部"),
    limit: int = Query(50, ge=1, le=500, description="返回条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    admin: dict = Depends(require_admin),
):
    """查询充电桩状态变更历史"""
    async with AsyncSessionLocal() as session:
        stmt = select(PileStatusLog).order_by(
            PileStatusLog.changed_at.desc())

        if pile_id:
            stmt = stmt.where(PileStatusLog.pile_id == pile_id)

        # 查总数
        count_stmt = select(func.count()).select_from(PileStatusLog)
        if pile_id:
            count_stmt = count_stmt.where(PileStatusLog.pile_id == pile_id)
        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        stmt = stmt.offset(offset).limit(limit)
        result = await session.execute(stmt)
        logs = result.scalars().all()

    return PileStatusLogResponse(
        logs=[
            PileStatusLogItem(
                id=log.id,
                pile_id=log.pile_id,
                old_status=log.old_status,
                new_status=log.new_status,
                reason=log.reason,
                operator=log.operator or "system",
                changed_at=log.changed_at,
            ) for log in logs
        ],
        total=total,
    )


# ---- 计费配置 ----

class BillingConfigUpdate(BaseModel):
    peak_rate: Optional[float] = None
    flat_rate: Optional[float] = None
    valley_rate: Optional[float] = None
    service_fee_rate: Optional[float] = None
    peak_hours: Optional[List[List[int]]] = None
    flat_hours: Optional[List[List[int]]] = None
    valley_hours: Optional[List[List[int]]] = None


@router.get("/billing-config")
async def get_billing_config_api(
    admin: dict = Depends(require_admin),
):
    """获取当前计费配置"""
    return get_billing_config()


@router.put("/billing-config")
async def update_billing_config_api(
    body: BillingConfigUpdate,
    admin: dict = Depends(require_admin),
):
    """动态更新计费配置（费率/时段）"""
    new_config = body.model_dump(exclude_none=True)
    if not new_config:
        raise HTTPException(status_code=400, detail="未提供任何更新字段")
    update_billing_config(new_config)
    return {"status": "success", "message": "计费配置已更新", "config": get_billing_config()}


# ---- 订单列表 ----

@router.get("/orders", response_model=AdminOrderListResponse)
async def admin_list_orders(
    status: str = Query(None, description="筛选状态"),
    vehicle_id: str = Query(None, description="筛选车牌号"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: dict = Depends(require_admin),
):
    """管理员查询所有订单"""
    async with AsyncSessionLocal() as session:
        stmt = select(ChargeOrder).order_by(ChargeOrder.id.desc())
        count_stmt = select(func.count()).select_from(ChargeOrder)

        if status:
            stmt = stmt.where(ChargeOrder.status == status)
            count_stmt = count_stmt.where(ChargeOrder.status == status)
        if vehicle_id:
            stmt = stmt.where(ChargeOrder.vehicle_id == vehicle_id)
            count_stmt = count_stmt.where(ChargeOrder.vehicle_id == vehicle_id)

        total = (await session.execute(count_stmt)).scalar() or 0
        result = await session.execute(stmt.offset(offset).limit(limit))
        orders = result.scalars().all()

    return AdminOrderListResponse(
        orders=[
            AdminOrderItem(
                order_id=o.id,
                vehicle_id=o.vehicle_id,
                charge_type=o.charge_type,
                requested_kwh=o.requested_kwh or 0.0,
                charged_kwh=o.charged_kwh or 0.0,
                queue_number=o.queue_number,
                status=o.status,
                pile_id=o.pile_id,
                total_fee=o.total_fee,
                created_at=o.created_at,
                started_at=o.started_at,
                finished_at=o.finished_at,
            ) for o in orders
        ],
        total=total,
    )


# ---- 账单列表（含详单） ----

@router.get("/bills", response_model=BillListResponse)
async def admin_list_bills(
    vehicle_id: str = Query(None, description="筛选车牌号"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: dict = Depends(require_admin),
):
    """管理员查询所有账单（含详单明细）"""
    async with AsyncSessionLocal() as session:
        stmt = (select(Bill)
                .options(selectinload(Bill.details))
                .order_by(Bill.id.desc()))
        count_stmt = select(func.count()).select_from(Bill)

        if vehicle_id:
            stmt = stmt.where(Bill.vehicle_id == vehicle_id)
            count_stmt = count_stmt.where(Bill.vehicle_id == vehicle_id)

        total = (await session.execute(count_stmt)).scalar() or 0
        result = await session.execute(stmt.offset(offset).limit(limit))
        bills = result.scalars().unique().all()

    return BillListResponse(
        bills=[
            BillItem(
                id=b.id,
                bill_code=b.bill_code,
                order_id=b.order_id,
                vehicle_id=b.vehicle_id,
                pile_id=b.pile_id,
                charge_type=b.charge_type,
                charge_start_time=b.charge_start_time,
                charge_end_time=b.charge_end_time,
                charge_duration=b.charge_duration,
                total_power=b.total_power,
                power_fee=b.power_fee,
                service_fee=b.service_fee,
                total_fee=b.total_fee,
                created_at=b.created_at,
                details=[
                    BillDetailItem(
                        id=d.id,
                        period=d.period,
                        start_time=d.start_time or "",
                        end_time=d.end_time or "",
                        duration_minutes=d.duration_minutes or 0,
                        kwh=d.kwh or 0.0,
                        rate=d.rate or 0.0,
                        fee=d.fee or 0.0,
                    ) for d in (b.details or [])
                ],
            ) for b in bills
        ],
        total=total,
    )


# ---- CSV 导出 ----

@router.get("/export/orders")
async def export_orders_csv(
    admin: dict = Depends(require_admin),
):
    """导出所有订单为 CSV 文件"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChargeOrder).order_by(ChargeOrder.id.desc()))
        orders = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "订单ID", "车牌号", "充电桩", "充电模式", "请求电量(kWh)",
        "实际电量(kWh)", "排队号", "状态", "充电费(元)", "服务费(元)",
        "总费用(元)", "创建时间", "开始充电", "完成时间",
    ])
    for o in orders:
        writer.writerow([
            o.id, o.vehicle_id, o.pile_id or "", o.charge_type,
            o.requested_kwh or 0, o.charged_kwh or 0,
            o.queue_number or "", o.status,
            o.power_fee or 0, o.service_fee or 0, o.total_fee or 0,
            o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
            o.charge_start_time.strftime("%Y-%m-%d %H:%M:%S") if o.charge_start_time else "",
            o.finished_at.strftime("%Y-%m-%d %H:%M:%S") if o.finished_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.get("/export/bills")
async def export_bills_csv(
    admin: dict = Depends(require_admin),
):
    """导出所有账单为 CSV 文件"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Bill)
            .options(selectinload(Bill.details))
            .order_by(Bill.id.desc()))
        bills = result.scalars().unique().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "账单编号", "订单ID", "车牌号", "充电桩", "充电模式",
        "充电时长(h)", "充电电量(kWh)", "充电费(元)", "服务费(元)",
        "总费用(元)", "开始时间", "结束时间", "账单生成时间",
    ])
    for b in bills:
        writer.writerow([
            b.bill_code, b.order_id, b.vehicle_id, b.pile_id or "",
            b.charge_type, round(b.charge_duration or 0, 4),
            round(b.total_power or 0, 4),
            round(b.power_fee or 0, 2), round(b.service_fee or 0, 2),
            round(b.total_fee or 0, 2),
            b.charge_start_time.strftime("%Y-%m-%d %H:%M:%S") if b.charge_start_time else "",
            b.charge_end_time.strftime("%Y-%m-%d %H:%M:%S") if b.charge_end_time else "",
            b.created_at.strftime("%Y-%m-%d %H:%M:%S") if b.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bills.csv"},
    )


@router.get("/export/bill-details")
async def export_bill_details_csv(
    admin: dict = Depends(require_admin),
):
    """导出所有详单为 CSV 文件"""
    async with AsyncSessionLocal() as session:
        stmt = (select(BillDetail, Bill.bill_code, Bill.vehicle_id)
                .join(Bill, BillDetail.bill_id == Bill.id)
                .order_by(BillDetail.id.desc()))
        result = await session.execute(stmt)
        rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "详单ID", "账单编号", "车牌号", "时段类型",
        "开始时刻", "结束时刻", "持续(分钟)",
        "电量(kWh)", "电价(元/kWh)", "费用(元)",
    ])
    for detail, bill_code, vehicle_id in rows:
        period_name = {"peak": "峰时", "flat": "平时", "valley": "谷时"}.get(
            detail.period, detail.period)
        writer.writerow([
            detail.id, bill_code, vehicle_id, period_name,
            detail.start_time or "", detail.end_time or "",
            detail.duration_minutes or 0,
            round(detail.kwh or 0, 4), round(detail.rate or 0, 2),
            round(detail.fee or 0, 2),
        ])

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bill_details.csv"},
    )
