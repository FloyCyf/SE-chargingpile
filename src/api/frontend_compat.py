from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from src.models.database import AsyncSessionLocal
from src.models.models import ChargeOrder
from datetime import datetime

router = APIRouter()

class ChargeReqBody(BaseModel):
    vehicle_id: str
    charge_type: str
    requested_capacity: float

@router.post("/user/charge/request")
async def user_charge_request(body: ChargeReqBody, request: Request):
    from src.api.schemas import ChargeRequest
    real_req = ChargeRequest(
        vehicle_id=body.vehicle_id,
        charge_type=body.charge_type,
        requested_kwh=body.requested_capacity
    )
    result = await request.app.state.scheduler.submit_request(real_req)
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result

@router.get("/user/vehicle/status/{vehicle_id}")
async def get_vehicle_status(vehicle_id: str, request: Request):
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    qdata = scheduler.get_waiting_area()
    
    for idx, q in enumerate(qdata.get("fast_waiting", [])):
        if q["vehicle_id"] == vehicle_id:
            return {"status": "WAITING", "queue_id": "F", "queue_position": idx, "order_id": q["order_id"], "estimated_wait_time": "15"}
            
    for idx, q in enumerate(qdata.get("slow_waiting", [])):
        if q["vehicle_id"] == vehicle_id:
            return {"status": "WAITING", "queue_id": "T", "queue_position": idx, "order_id": q["order_id"], "estimated_wait_time": "30"}
            
    for p in status.get("piles", []):
        for idx, q in enumerate(p.get("queue_items", [])):
            if q["vehicle_id"] == vehicle_id:
                state = "CHARGING" if idx == 0 and p["status"] == "CHARGING" else "QUEUING"
                return {"status": state, "queue_id": p["pile_id"], "queue_position": idx, "order_id": q["order_id"], "estimated_wait_time": "0"}
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChargeOrder).where(ChargeOrder.vehicle_id == vehicle_id).order_by(ChargeOrder.id.desc())
        )
        order = result.scalars().first()
        if order and order.status in ["COMPLETED", "FAULTED"]:
            return {"status": order.status, "order_id": order.id, "queue_id": order.pile_id, "queue_position": 0, "estimated_wait_time": "-"}
            
    raise HTTPException(404, "Vehicle not found")

class CancelReqBody(BaseModel):
    vehicle_id: str

@router.post("/user/charge/cancel")
async def cancel_charge(body: CancelReqBody, request: Request):
    vehicle_id = body.vehicle_id
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    qdata = scheduler.get_waiting_area()
    oid = None
    
    for q in qdata.get("fast_waiting", []):
        if q["vehicle_id"] == vehicle_id: oid = q["order_id"]
    for q in qdata.get("slow_waiting", []):
        if q["vehicle_id"] == vehicle_id: oid = q["order_id"]
    for p in status.get("piles", []):
        for q in p.get("queue_items", []):
            if q["vehicle_id"] == vehicle_id: oid = q["order_id"]
            
    if oid:
        res = await scheduler.cancel_request(oid)
        if res.get('status') == 'failed':
            raise HTTPException(status_code=400, detail=res.get("message"))
    return {"status":"success"}

@router.get("/queue/list")
async def get_queue_list(request: Request):
    status = request.app.state.scheduler.get_system_status()
    result = []
    for q in status.get("fast_queue", []):
        result.append({"queue_id": "F", "vehicle_id": q["vehicle_id"], "queue_duration": q.get('queue_duration', 0), "requested_capacity": q.get('requested_kwh', 0)})
    for q in status.get("slow_queue", []):
        result.append({"queue_id": "T", "vehicle_id": q["vehicle_id"], "queue_duration": q.get('queue_duration', 0), "requested_capacity": q.get('requested_kwh', 0)})
    return result

@router.get("/pile/status")
async def get_pile_status(request: Request):
    status = request.app.state.scheduler.get_system_status()
    res = []
    for p in status.get("piles", []):
        is_charging = p["status"] == "CHARGING"
        qi = p.get("queue_items", [])
        current_user = qi[0]["vehicle_id"] if is_charging and qi else "暂无"
        cur_soc = p.get("current_soc", 0.0)
        dur = p.get("charged_duration", 0)
        
        # calculate roughly electricity if not exposed yet
        res.append({
            "pile_id": p["pile_id"],
            "status": "充电中" if is_charging else ("故障" if p["status"]=="FAULT" else "空闲"),
            "current_user": current_user,
            "charged_electricity": cur_soc*100 if is_charging else 0, # rough display
            "charged_duration": dur or 0
        })
    return res

@router.get("/user/orders/{vehicle_id}")
async def get_orders(vehicle_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChargeOrder).where(ChargeOrder.vehicle_id==vehicle_id).order_by(ChargeOrder.id.desc()))
        orders = result.scalars().all()
    out = []
    for o in orders:
        out.append({
            "order_id": o.id, "pile_id": o.pile_id, "electricity": o.total_power or 0,
            "duration": o.charge_duration or 0, "total_fee": o.total_fee or 0,
            "start_time": o.created_at.isoformat()
        })
    return out

@router.get("/order/{order_id}")
async def get_order_detail(order_id: int):
    async with AsyncSessionLocal() as session:
        o = await session.get(ChargeOrder, order_id)
    if not o: raise HTTPException(404)
    return {
        "order_id": o.id, "pile_id": o.pile_id, "electricity": o.total_power or 0,
        "duration": o.charge_duration or 0, "electricity_fee": o.power_fee or 0,
        "service_fee": o.service_fee or 0, "total_fee": o.total_fee or 0
    }

@router.get("/compat/dump")
async def compat_dump(request: Request):
    scheduler = request.app.state.scheduler
    status = scheduler.get_system_status()
    for p in status["piles"]:
        isC = p["status"] == "CHARGING"
        qi = p.get("queue_items", [])
        if isC and qi:
            p["vehicle_id"] = qi[0]["vehicle_id"]
            req = qi[0].get("requested_kwh") or 1
            p["current_soc"] = qi[0].get("charged_kwh", 0) / req
            p["target_soc"] = 1.0
        else:
            p["vehicle_id"] = "undef"
            p["current_soc"] = 0
            p["target_soc"] = 1.0
    
    qdata = scheduler.get_waiting_area()
    status["fast_queue"] = []
    for q in qdata["fast_waiting"]:
        dur = 0
        if "waiting_since" in q and q["waiting_since"]:
            dur = (datetime.now() - q["waiting_since"]).total_seconds()
        status["fast_queue"].append({
            "vehicle_id": q["vehicle_id"],
            "queue_num": q["queue_number"],
            "requested_capacity": q["requested_kwh"],
            "queue_duration": dur,
            "battery_capacity": 100,
        })
    status["slow_queue"] = []
    for q in qdata["slow_waiting"]:
        dur = 0
        if "waiting_since" in q and q["waiting_since"]:
            dur = (datetime.now() - q["waiting_since"]).total_seconds()
        status["slow_queue"].append({
            "vehicle_id": q["vehicle_id"],
            "queue_num": q["queue_number"],
            "requested_capacity": q["requested_kwh"],
            "queue_duration": dur,
            "battery_capacity": 100,
        })
    status["fast_queue_count"] = status["fast_waiting_count"]
    status["slow_queue_count"] = status["slow_waiting_count"]
    return status
