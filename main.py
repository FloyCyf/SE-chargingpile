from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
import random

# 创建SQLite数据库引擎
engine = create_engine('sqlite:///charging.db')
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 数据库模型
class ChargingOrder(Base):
    __tablename__ = "charging_orders"
    order_id = Column(String, primary_key=True, index=True)
    pile_id = Column(String, index=True)
    electricity = Column(Float)
    duration = Column(Integer)  # 秒
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    electricity_fee = Column(Float)
    service_fee = Column(Float)
    total_fee = Column(Float)
    payment_status = Column(String)  # 未支付/已支付

class PileStatus(Base):
    __tablename__ = "pile_status"
    pile_id = Column(String, primary_key=True, index=True)
    status = Column(String)  # 空闲/充电中/故障
    current_user = Column(String, nullable=True)
    charging_duration = Column(Integer, default=0)  # 秒
    charging_electricity = Column(Float, default=0)

class QueueInfo(Base):
    __tablename__ = "queue_info"
    queue_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True)
    requested_electricity = Column(Float)
    queue_duration = Column(Integer)  # 秒
    queue_status = Column(String)  # 排队中/已完成

# 创建数据库表
Base.metadata.create_all(bind=engine)

# 初始化数据
def init_data():
    db = SessionLocal()
    try:
        # 检查是否已有数据
        if db.query(PileStatus).count() == 0:
            # 创建10个充电桩
            for i in range(1, 11):
                pile = PileStatus(
                    pile_id=f"P{i:03d}",
                    status=random.choice(["空闲", "充电中", "故障"]),
                    current_user=f"User{random.randint(100, 999)}" if random.choice([True, False]) else None,
                    charging_duration=random.randint(0, 3600),
                    charging_electricity=round(random.uniform(0, 50), 2)
                )
                db.add(pile)
            db.commit()
        
        if db.query(ChargingOrder).count() == 0:
            # 创建一些历史订单
            for i in range(1, 6):
                start_time = datetime.now() - timedelta(hours=random.randint(1, 24))
                end_time = start_time + timedelta(minutes=random.randint(30, 120))
                electricity = round(random.uniform(5, 50), 2)
                electricity_fee = round(electricity * 0.8, 2)
                service_fee = round(electricity * 0.1, 2)
                order = ChargingOrder(
                    order_id=f"ORD{i:06d}",
                    pile_id=f"P{random.randint(1, 10):03d}",
                    electricity=electricity,
                    duration=int((end_time - start_time).total_seconds()),
                    start_time=start_time,
                    end_time=end_time,
                    electricity_fee=electricity_fee,
                    service_fee=service_fee,
                    total_fee=round(electricity_fee + service_fee, 2),
                    payment_status=random.choice(["未支付", "已支付"])
                )
                db.add(order)
            db.commit()
        
        if db.query(QueueInfo).count() == 0:
            # 创建一些排队信息
            for i in range(1, 6):
                queue = QueueInfo(
                    queue_id=f"QUE{i:04d}",
                    user_id=f"User{random.randint(100, 999)}",
                    requested_electricity=round(random.uniform(10, 50), 2),
                    queue_duration=random.randint(0, 1800),
                    queue_status=random.choice(["排队中", "已完成"])
                )
                db.add(queue)
            db.commit()
    finally:
        db.close()

# 创建FastAPI应用
app = FastAPI()

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生产环境中应该设置具体的前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API接口
@app.get("/api/order/{order_id}")
def get_order(order_id: str):
    db = SessionLocal()
    try:
        order = db.query(ChargingOrder).filter(ChargingOrder.order_id == order_id).first()
        if not order:
            return {"error": "订单不存在"}
        return {
            "order_id": order.order_id,
            "pile_id": order.pile_id,
            "electricity": order.electricity,
            "duration": order.duration,
            "start_time": order.start_time.isoformat(),
            "end_time": order.end_time.isoformat(),
            "electricity_fee": order.electricity_fee,
            "service_fee": order.service_fee,
            "total_fee": order.total_fee,
            "payment_status": order.payment_status
        }
    finally:
        db.close()

@app.get("/api/pile/status")
def get_pile_status():
    db = SessionLocal()
    try:
        piles = db.query(PileStatus).all()
        return [
            {
                "pile_id": pile.pile_id,
                "status": pile.status,
                "current_user": pile.current_user,
                "charging_duration": pile.charging_duration,
                "charging_electricity": pile.charging_electricity
            }
            for pile in piles
        ]
    finally:
        db.close()

@app.get("/api/pile/statistics")
def get_pile_statistics():
    db = SessionLocal()
    try:
        total_piles = db.query(PileStatus).count()
        free_piles = db.query(PileStatus).filter(PileStatus.status == "空闲").count()
        charging_piles = db.query(PileStatus).filter(PileStatus.status == "充电中").count()
        fault_piles = db.query(PileStatus).filter(PileStatus.status == "故障").count()
        
        # 计算今日数据
        today = datetime.now().date()
        today_orders = db.query(ChargingOrder).filter(
            ChargingOrder.start_time >= datetime(today.year, today.month, today.day)
        ).all()
        today_charging_count = len(today_orders)
        today_electricity = sum(order.electricity for order in today_orders)
        today_revenue = sum(order.total_fee for order in today_orders)
        
        return {
            "total_piles": total_piles,
            "free_piles": free_piles,
            "charging_piles": charging_piles,
            "fault_piles": fault_piles,
            "today_charging_count": today_charging_count,
            "today_electricity": round(today_electricity, 2),
            "today_revenue": round(today_revenue, 2)
        }
    finally:
        db.close()

@app.get("/api/queue/list")
def get_queue_list():
    db = SessionLocal()
    try:
        queues = db.query(QueueInfo).all()
        return [
            {
                "queue_id": queue.queue_id,
                "user_id": queue.user_id,
                "requested_electricity": queue.requested_electricity,
                "queue_duration": queue.queue_duration,
                "queue_status": queue.queue_status
            }
            for queue in queues
        ]
    finally:
        db.close()

@app.post("/api/pay/{order_id}")
def pay_order(order_id: str):
    db = SessionLocal()
    try:
        order = db.query(ChargingOrder).filter(ChargingOrder.order_id == order_id).first()
        if not order:
            return {"error": "订单不存在"}
        if order.payment_status == "已支付":
            return {"message": "订单已支付"}
        
        # 模拟支付
        order.payment_status = "已支付"
        db.commit()
        return {"message": "支付成功"}
    finally:
        db.close()

# 初始化数据
init_data()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)