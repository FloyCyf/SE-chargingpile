from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class ChargeRequest(BaseModel):
    vehicle_id: str = Field(..., description="车牌号")
    charge_type: str = Field(..., description="Fast 或是 Slow")
    current_soc: float = Field(..., ge=0.0, le=1.0, description="当前电量比例")
    target_soc: float = Field(..., ge=0.0, le=1.0, description="目标电量比例")


class ChargeResponse(BaseModel):
    status: str
    message: str
    queue_position: Optional[int] = None
    assigned_pile: Optional[str] = None


class PileStatus(BaseModel):
    pile_id: str
    type: str
    status: str
    vehicle_id: Optional[str] = None
    current_soc: Optional[float] = None
    target_soc: Optional[float] = None
    total_charge_count: int = 0
    total_charge_duration: float = 0.0
    total_charge_amount: float = 0.0


class SystemStatusResponse(BaseModel):
    piles: list[PileStatus]
    fast_queue_count: int
    slow_queue_count: int


# ---- 计费与状态操作相关模型 ----

class FeeDetail(BaseModel):
    """分时计费明细"""
    peak_minutes: int = 0
    flat_minutes: int = 0
    valley_minutes: int = 0


class BillResponse(BaseModel):
    """订单账单详情"""
    order_id: int
    vehicle_id: str
    pile_id: Optional[str] = None
    charge_type: str
    status: str

    start_soc: float
    target_soc: float

    bill_code: Optional[str] = None
    charge_start_time: Optional[datetime] = None
    charge_end_time: Optional[datetime] = None
    charge_duration: Optional[float] = None
    total_power: Optional[float] = None

    power_fee: Optional[float] = None
    service_fee: Optional[float] = None
    total_fee: Optional[float] = None

    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    detail: Optional[FeeDetail] = None


class CancelResponse(BaseModel):
    """取消排队结果"""
    status: str
    message: str


class StopResponse(BaseModel):
    """主动停止充电结果"""
    status: str
    message: str
    total_power: Optional[float] = None
    power_fee: Optional[float] = None
    service_fee: Optional[float] = None
    total_fee: Optional[float] = None
