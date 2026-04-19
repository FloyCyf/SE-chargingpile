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
print("数据库表创建成功")

# 初始化数据
db = SessionLocal()
try:
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
    print("充电桩数据初始化成功")
    
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
    print("订单数据初始化成功")
    
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
    print("排队数据初始化成功")
finally:
    db.close()

print("测试完成")