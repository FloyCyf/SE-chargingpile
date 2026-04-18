from pydantic import BaseModel, Field
from typing import List, Optional
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
    type: str      # Fast or Slow
    status: str    # IDLE or CHARGING
    vehicle_id: Optional[str] = None
    current_soc: Optional[float] = None
    target_soc: Optional[float] = None


class SystemStatusResponse(BaseModel):
    piles: List[PileStatus]
    fast_queue_count: int
    slow_queue_count: int


# ---- 计费与状态操作相关模型 ----

class FeeDetailItem(BaseModel):
    """分时计费明细条目"""
    period: str    # 波峰 / 波平 / 波谷
    rate: float    # 该时段费率（元/度）
    minutes: float # 该时段充电时长（分钟）
    kwh: float     # 该时段充入度数
    fee: float     # 该时段费用（元）


class BillResponse(BaseModel):
    """订单账单详情"""
    order_id: int
    vehicle_id: str
    pile_id: Optional[str] = None
    charge_type: str
    status: str

    start_soc: float
    end_soc: Optional[float] = None
    target_soc: float
    charge_kwh: Optional[float] = None

    electricity_fee: Optional[float] = None
    service_fee: Optional[float] = None
    timeout_fee: Optional[float] = None
    total_fee: Optional[float] = None

    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    left_at: Optional[datetime] = None

    fee_detail: Optional[List[FeeDetailItem]] = None


class CancelResponse(BaseModel):
    """取消排队结果"""
    status: str
    message: str


class StopResponse(BaseModel):
    """主动停止充电结果"""
    status: str
    message: str
    charge_kwh: Optional[float] = None
    electricity_fee: Optional[float] = None
    service_fee: Optional[float] = None
    total_fee: Optional[float] = None
