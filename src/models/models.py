from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class ChargeOrder(Base):
    __tablename__ = 'charge_orders'

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(String(50), nullable=False)
    pile_id = Column(String(50), nullable=True)    # 未分到桩排队时为 None
    charge_type = Column(String(10), nullable=False) # 'Fast' or 'Slow'
    start_soc = Column(Float, nullable=False)
    target_soc = Column(Float, nullable=False)

    # 状态机：QUEUING -> CHARGING -> COMPLETED / INTERRUPTED / CANCELLED
    status = Column(String(20), default='QUEUING')

    # 各个里程碑时间点（全量使用虚拟相对时钟记录）
    created_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    left_at = Column(DateTime, nullable=True)      # 车辆实际离开时间（用于超时计算）

    # 充电结果与计费字段
    end_soc = Column(Float, nullable=True)          # 充电结束时的实际电量
    charge_kwh = Column(Float, nullable=True)       # 本次充入的总度数（千瓦时）
    electricity_fee = Column(Float, nullable=True)  # 分时阶梯电费（元）
    service_fee = Column(Float, nullable=True)      # 充电服务费（元）
    timeout_fee = Column(Float, default=0.0)        # 超时占位费（元）
    total_fee = Column(Float, nullable=True)        # 最终应付总费用（元）
