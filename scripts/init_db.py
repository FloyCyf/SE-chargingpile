import argparse
import asyncio
import time
from pathlib import Path

from src.loader import config_data
from src.models.database import init_db, seed_admin, seed_charging_piles


async def _run(reset: bool):
    db_path = Path("charging.db")

    if reset and db_path.exists():
        # Try unlink first (fast path when no one holds the file)
        try:
            db_path.unlink()
            print("DB file deleted, recreating from scratch.")
            await init_db()
            await seed_admin()
            await seed_charging_piles(config_data)
            return
        except PermissionError:
            print("DB file is locked by another process, will drop tables via SQL instead ...")

        # Fallback: connect and drop all tables via SQLAlchemy
        # Retry up to 5 times in case of transient locks
        for attempt in range(5):
            try:
                from src.models.database import engine, Base
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.drop_all)
                await init_db()
                await seed_admin()
                await seed_charging_piles(config_data)
                print("Tables dropped and recreated via SQL.")
                return
            except Exception as e:
                if attempt < 4:
                    wait = (attempt + 1) * 2
                    print(f"  Retry {attempt + 1}/5 after {wait}s (error: {e}) ...")
                    time.sleep(wait)
                else:
                    raise
    else:
        await init_db()
        await seed_admin()
        await seed_charging_piles(config_data)


def main():
    parser = argparse.ArgumentParser(
        description="Initialize SQLite tables and seed configured charging piles.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete charging.db before initialization.")
    args = parser.parse_args()
    asyncio.run(_run(reset=args.reset))
    print("Database initialized and charging piles seeded.")


if __name__ == "__main__":
    main()
