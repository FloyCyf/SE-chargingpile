from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ---- 认证相关 ----

class UserRegister(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4)
    vehicle_id: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    username: str


class UserInfo(BaseModel):
    user_id: int
    username: str
    role: str
    vehicle_id: Optional[str] = None


# ---- 车辆管理 ----

class VehicleCreate(BaseModel):
    vehicle_id: str = Field(..., description="车牌号")
    battery_capacity_kwh: float = Field(..., gt=0, description="电池最大容量(kWh)")
    current_kwh: float = Field(0.0, ge=0, description="当前电池电量(kWh)")


class VehicleUpdate(BaseModel):
    battery_capacity_kwh: Optional[float] = Field(None, gt=0, description="电池最大容量(kWh)")
    current_kwh: Optional[float] = Field(None, ge=0, description="当前电池电量(kWh)")


class VehicleResponse(BaseModel):
    vehicle_id: str
    battery_capacity_kwh: float
    current_kwh: float
    owner_id: Optional[int] = None


class VehicleListResponse(BaseModel):
    vehicles: List[VehicleResponse]


# ---- 充电请求 ----

class ChargeRequest(BaseModel):
    vehicle_id: str = Field(..., description="车牌号")
    charge_type: str = Field(..., description="Fast 或 Slow")
    requested_kwh: float = Field(..., gt=0, description="请求充电量(度)")


class ChargeResponse(BaseModel):
    status: str
    message: str
    order_id: Optional[int] = None
    queue_number: Optional[str] = None
    queue_position: Optional[int] = None
    assigned_pile: Optional[str] = None


# ---- 修改请求 ----

class ModifyRequest(BaseModel):
    charge_type: Optional[str] = Field(None, description="新充电模式 Fast/Slow")
    requested_kwh: Optional[float] = Field(None, gt=0, description="新充电量(度)")


class ModifyResponse(BaseModel):
    status: str
    message: str
    new_queue_number: Optional[str] = None


# ---- 充电桩状态 ----

class PileQueueItemDetail(BaseModel):
    """桩队列中每辆车的详细信息"""
    position: int
    order_id: int
    vehicle_id: str
    queue_number: Optional[str] = None
    requested_kwh: float
    charged_kwh: float = 0.0
    user_id: Optional[int] = None
    wait_duration_minutes: Optional[float] = None


class PileStatus(BaseModel):
    pile_id: str
    type: str
    status: str
    power: float = 0.0
    queue_len: int = 0
    max_queue_len: int = 0
    total_charge_count: int = 0
    total_charge_duration: float = 0.0
    total_charge_amount: float = 0.0
    total_power_fee: float = 0.0
    total_service_fee: float = 0.0
    total_total_fee: float = 0.0
    queue_items: List[PileQueueItemDetail] = []


class SystemStatusResponse(BaseModel):
    piles: List[PileStatus]
    fast_waiting_count: int
    slow_waiting_count: int


# ---- 队列位置查询 ----

class QueuePositionResponse(BaseModel):
    order_id: int
    queue_number: Optional[str] = None
    status: str
    ahead_count: int = 0
    pile_id: Optional[str] = None
    charge_type: str
    requested_kwh: float


# ---- 计费与账单 ----

class FeeDetail(BaseModel):
    peak_minutes: int = 0
    flat_minutes: int = 0
    valley_minutes: int = 0


class BillResponse(BaseModel):
    order_id: int
    vehicle_id: str
    pile_id: Optional[str] = None
    charge_type: str
    status: str

    requested_kwh: float = 0.0
    queue_number: Optional[str] = None

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


# ---- 取消 / 停止 ----

class CancelResponse(BaseModel):
    status: str
    message: str


class StopResponse(BaseModel):
    status: str
    message: str
    total_power: Optional[float] = None
    power_fee: Optional[float] = None
    service_fee: Optional[float] = None
    total_fee: Optional[float] = None


# ---- 管理员操作 ----

class PileControlRequest(BaseModel):
    action: str = Field(..., description="start / stop / fault")


class PileControlResponse(BaseModel):
    status: str
    message: str
    pile_id: str


# ---- 报表 ----

class ReportItem(BaseModel):
    pile_id: str
    pile_type: str
    charge_count: int = 0
    total_duration: float = 0.0
    total_kwh: float = 0.0
    total_power_fee: float = 0.0
    total_service_fee: float = 0.0
    total_total_fee: float = 0.0


class ReportResponse(BaseModel):
    period: str
    start_date: str
    end_date: str
    items: List[ReportItem]


# ---- 等候区车辆 ----

class WaitingCarDetail(BaseModel):
    order_id: int
    vehicle_id: str
    queue_number: str
    charge_type: str
    requested_kwh: float
    user_id: Optional[int] = None
    waiting_since: Optional[datetime] = None


class WaitingAreaResponse(BaseModel):
    fast_waiting: List[WaitingCarDetail]
    slow_waiting: List[WaitingCarDetail]


# ---- 订单列表 ----

class OrderSummary(BaseModel):
    order_id: int
    vehicle_id: str
    charge_type: str
    requested_kwh: float
    queue_number: Optional[str] = None
    status: str
    pile_id: Optional[str] = None
    total_fee: Optional[float] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class OrderListResponse(BaseModel):
    orders: List[OrderSummary]
