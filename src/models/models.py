from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, func
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

    vehicles = relationship("Vehicle", backref="owner", foreign_keys="Vehicle.owner_id")


class Vehicle(Base):
    """车辆表：记录车辆信息及电池最大充电容量"""
    __tablename__ = 'vehicles'

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(String(50), unique=True, nullable=False, comment="车牌号")
    battery_capacity_kwh = Column(Float, nullable=False, default=60.0,
                                  comment="电池最大容量(kWh)")
    current_kwh = Column(Float, default=0.0, comment="当前电池电量(kWh)")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True,
                      comment="车主用户ID")


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


class PileStatusLog(Base):
    """充电桩状态变更日志表"""
    __tablename__ = 'pile_status_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    pile_id = Column(String(50), nullable=False, comment="充电桩编号")
    old_status = Column(String(20), nullable=False, comment="变更前状态")
    new_status = Column(String(20), nullable=False, comment="变更后状态")
    reason = Column(String(200), nullable=True,
                    comment="变更原因(调度/故障/手动操作/充电完成等)")
    operator = Column(String(50), default="system",
                      comment="操作者(system/管理员用户名)")
    changed_at = Column(DateTime, default=func.now(), comment="变更时间")


class Bill(Base):
    """账单表：每笔已完成充电的费用汇总"""
    __tablename__ = 'bills'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bill_code = Column(String(32), unique=True, nullable=False, comment="账单编号")
    order_id = Column(Integer, ForeignKey("charge_orders.id"), nullable=False,
                      comment="关联订单ID")
    vehicle_id = Column(String(50), nullable=False, comment="车牌号")
    pile_id = Column(String(50), nullable=True, comment="充电桩编号")
    charge_type = Column(String(10), nullable=False, comment="Fast/Slow")

    charge_start_time = Column(DateTime, comment="充电启动时间")
    charge_end_time = Column(DateTime, comment="充电结束时间")
    charge_duration = Column(Float, comment="充电时长(小时)")
    total_power = Column(Float, comment="充电电量(kWh)")

    power_fee = Column(Float, default=0.0, comment="充电费用")
    service_fee = Column(Float, default=0.0, comment="服务费用")
    total_fee = Column(Float, default=0.0, comment="总费用")

    created_at = Column(DateTime, default=func.now(), comment="账单生成时间")

    order = relationship("ChargeOrder", backref="bill", foreign_keys=[order_id])


class BillDetail(Base):
    """详单表：账单中每个时段的费用明细"""
    __tablename__ = 'bill_details'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bill_id = Column(Integer, ForeignKey("bills.id"), nullable=False,
                     comment="关联账单ID")
    period = Column(String(10), nullable=False, comment="时段类型: peak/flat/valley")
    start_time = Column(String(10), comment="时段开始时刻 HH:MM")
    end_time = Column(String(10), comment="时段结束时刻 HH:MM")
    duration_minutes = Column(Integer, default=0, comment="该段持续分钟数")
    kwh = Column(Float, default=0.0, comment="该段充电量(kWh)")
    rate = Column(Float, default=0.0, comment="该段电价(元/kWh)")
    fee = Column(Float, default=0.0, comment="该段充电费用(元)")

    bill = relationship("Bill", backref="details", foreign_keys=[bill_id])
