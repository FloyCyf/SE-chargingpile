from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class ChargeOrder(Base):
    __tablename__ = 'charge_orders'

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(String(50), nullable=False)
    pile_id = Column(String(50), nullable=True) # 未分到桩排队时为 None
    charge_type = Column(String(10), nullable=False) # 'Fast' or 'Slow'
    start_soc = Column(Float, nullable=False)
    target_soc = Column(Float, nullable=False)
    
    # 状态机约束：'QUEUING', 'CHARGING', 'COMPLETED'
    status = Column(String(20), default='QUEUING')
    
    # 各个里程碑时间点（全量使用虚拟相对时钟记录）
    created_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
