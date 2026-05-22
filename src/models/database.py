import hashlib
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.models.models import Base, ChargingPile, User

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
        result = await conn.execute(text("PRAGMA table_info(charge_orders)"))
        columns = {row[1] for row in result.fetchall()}
        if "requested_start_time" not in columns:
            await conn.execute(text(
                "ALTER TABLE charge_orders "
                "ADD COLUMN requested_start_time DATETIME"
            ))


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


async def seed_charging_piles(config: dict):
    """Ensure configured charging piles exist in the database."""
    system_cfg = config.get("system", {})
    fast_count = system_cfg.get(
        "fast_pile_count", system_cfg.get("fast_charging_piles", 3))
    slow_count = system_cfg.get(
        "slow_pile_count", system_cfg.get("slow_charging_piles", 2))

    expected = [
        (f"F{i + 1}", "Fast") for i in range(fast_count)
    ] + [
        (f"T{i + 1}", "Slow") for i in range(slow_count)
    ]

    async with AsyncSessionLocal() as session:
        for pile_id, pile_type in expected:
            result = await session.execute(
                select(ChargingPile).where(ChargingPile.pile_id == pile_id)
            )
            pile = result.scalars().first()
            if pile is None:
                session.add(ChargingPile(
                    pile_id=pile_id,
                    pile_type=pile_type,
                    status="IDLE",
                ))
            else:
                pile.pile_type = pile_type
                pile.status = pile.status or "IDLE"
        await session.commit()


async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session
