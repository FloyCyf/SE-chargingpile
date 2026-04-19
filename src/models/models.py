from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class OrderStatus:
    """订单状态常量"""
    WAITING = "WAITING"        # 在等候区等待
    QUEUING = "QUEUING"        # 在充电桩队列中排队（未充电）
    CHARGING = "CHARGING"      # 正在充电（桩队列 position 0）
    COMPLETED = "COMPLETED"    # 充电完成
    CANCELLED = "CANCELLED"    # 已取消
    FAULTED = "FAULTED"        # 因故障中断


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), default="user", comment="user 或 admin")
    vehicle_id = Column(String(50), nullable=True, comment="关联车牌号")
    created_at = Column(DateTime, default=datetime.utcnow)


class ChargeOrder(Base):
    __tablename__ = 'charge_orders'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    vehicle_id = Column(String(50), nullable=False)
    pile_id = Column(String(50), nullable=True)
    charge_type = Column(String(10), nullable=False, comment="Fast 或 Slow")
    requested_kwh = Column(Float, nullable=False, comment="请求充电量(度)")
    charged_kwh = Column(Float, default=0.0, comment="实际已充电量(度)")
    queue_number = Column(String(10), nullable=True, comment="排队号码 F1/T1")

    status = Column(String(20), default=OrderStatus.WAITING)

    created_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    left_at = Column(DateTime, nullable=True)

    # 计费相关字段
    bill_code = Column(String(32), unique=True, nullable=True, comment="详单编号")
    charge_start_time = Column(DateTime, comment="充电启动时间")
    charge_end_time = Column(DateTime, comment="充电停止时间")
    charge_duration = Column(Float, comment="充电时长(小时)")
    total_power = Column(Float, comment="充电电量(度)")
    power_fee = Column(Float, comment="充电费用")
    service_fee = Column(Float, comment="服务费用")
    total_fee = Column(Float, comment="总费用")

    # 关系
    user = relationship("User", backref="orders", foreign_keys=[user_id])


class ChargingPile(Base):
    __tablename__ = 'charging_piles'

    id = Column(Integer, primary_key=True, index=True)
    pile_id = Column(String(50), unique=True, nullable=False)
    pile_type = Column(String(10), nullable=False, comment="Fast 或 Slow")
    status = Column(String(20), default="IDLE")

    # 累计统计字段
    total_charge_count = Column(Integer, default=0, comment="累计充电次数")
    total_charge_duration = Column(Float, default=0.0, comment="累计充电时长(小时)")
    total_charge_amount = Column(Float, default=0.0, comment="累计充电量(度)")
    total_power_fee = Column(Float, default=0.0, comment="累计充电费用")
    total_service_fee = Column(Float, default=0.0, comment="累计服务费用")
    total_total_fee = Column(Float, default=0.0, comment="累计总费用")


class PileQueue(Base):
    __tablename__ = 'pile_queues'

    id = Column(Integer, primary_key=True, autoincrement=True)
    pile_id = Column(Integer, ForeignKey("charging_piles.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("charge_orders.id"), nullable=False)
    position = Column(Integer, nullable=False, comment="队列位置,0=正在充电")
    queue_number = Column(String(10), nullable=True, comment="排队号码")
    entered_at = Column(DateTime, default=datetime.utcnow)

    pile = relationship("ChargingPile", backref="queue_items")
    order = relationship("ChargeOrder", backref="pile_queue")
