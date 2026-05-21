import argparse
import asyncio
from pathlib import Path

from src.loader import config_data
from src.models.database import init_db, seed_admin, seed_charging_piles


async def _run(reset: bool):
    db_path = Path("charging.db")
    if reset and db_path.exists():
        db_path.unlink()

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
