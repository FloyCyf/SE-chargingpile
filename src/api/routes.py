from fastapi import APIRouter, Request
from src.api.schemas import ChargeRequest, ChargeResponse, SystemStatusResponse

router = APIRouter()

@router.post("/requests/", response_model=ChargeResponse)
async def submit_charge_request(req_body: ChargeRequest, request: Request):
    """提交充电请求: 分配桩资源或进入等待队列"""
    scheduler = request.app.state.scheduler
    # 调度过程已包含 DB 连接，需要 await
    result = await scheduler.submit_request(req_body)
    return ChargeResponse(**result)

@router.get("/system/dump", response_model=SystemStatusResponse)
async def get_site_status(request: Request):
    """(调试探测用) 获取充电站实时状态和队列长度"""
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    return SystemStatusResponse(**status)
