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
    type: str      # Fast or Slow
    status: str    # IDLE or CHARGING
    vehicle_id: Optional[str] = None
    current_soc: Optional[float] = None
    target_soc: Optional[float] = None


class SystemStatusResponse(BaseModel):
    piles: list[PileStatus]
    fast_queue_count: int
    slow_queue_count: int


# ---- 计费与状态操作相关模型 ----

class FeeDetail(BaseModel):
    """分时计费明细"""
    peak_kwh: float = 0.0    # 波峰充电度数
    flat_kwh: float = 0.0    # 波平充电度数
    valley_kwh: float = 0.0  # 波谷充电度数


class BillResponse(BaseModel):
    """订单账单详情"""
    order_id: int
    vehicle_id: str
    pile_id: Optional[str] = None
    charge_type: str
    status: str

    start_soc: float
    target_soc: float
    total_power: Optional[float] = None    # 总充电度数

    power_fee: Optional[float] = None      # 分时电费
    service_fee: Optional[float] = None    # 服务费
    total_fee: Optional[float] = None      # 总费用

    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    left_at: Optional[datetime] = None

    detail: Optional[FeeDetail] = None     # 分时明细


class CancelResponse(BaseModel):
    """取消排队结果"""
    status: str
    message: str


class StopResponse(BaseModel):
    """主动停止充电结果"""
    status: str
    message: str
    total_power: Optional[float] = None    # 总充电度数
    power_fee: Optional[float] = None      # 分时电费
    service_fee: Optional[float] = None    # 服务费
    total_fee: Optional[float] = None      # 总费用
