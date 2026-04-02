from pydantic import BaseModel, Field
from typing import List, Optional

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
    type: str # Fast or Slow
    status: str # IDLE or CHARGING
    vehicle_id: Optional[str] = None
    current_soc: Optional[float] = None
    target_soc: Optional[float] = None

class SystemStatusResponse(BaseModel):
    piles: List[PileStatus]
    fast_queue_count: int
    slow_queue_count: int
