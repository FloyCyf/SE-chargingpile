import hashlib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.models.models import Base, User

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
        await conn.run_sync(Base.metadata.create_all)


async def seed_admin():
    """确保默认管理员帐号存在"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == "admin")
        )
        if result.scalars().first() is None:
            salt = "scs_salt_2026"
            pw_hash = hashlib.sha256(("admin123" + salt).encode()).hexdigest()
            admin = User(
                username="admin",
                password_hash=pw_hash,
                role="admin",
                vehicle_id=None,
            )
            session.add(admin)
            await session.commit()


async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session
