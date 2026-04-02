from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.models.models import Base
import os

# 使用 aiosqlite 支持 SQLite 的异步环境能力
DB_PATH = "sqlite+aiosqlite:///./charging.db"

engine = create_async_engine(
    DB_PATH,
    echo=False,
    connect_args={"check_same_thread": False}
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession, 
    expire_on_commit=False,
    autoflush=False
)

async def init_db():
    async with engine.begin() as conn:
        # 直接由 SQLAlchemy 生成所有被扫描到的物理建库语句
        await conn.run_sync(Base.metadata.create_all)
        
async def get_db_session():
    """提供给 FastAPI Depends 依赖注入用的数据管道挂载"""
    async with AsyncSessionLocal() as session:
        yield session
