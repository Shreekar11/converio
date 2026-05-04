#!/usr/bin/env python3
"""Seed synthetic operator rows into PG.

Operators are Converio internal talent-ops users — not tied to any company.
`supabase_user_id` is null at seed time and is populated lazily on first
Supabase login (mirrors the recruiter pattern in `seed_recruiters.py`).

Idempotency: existing rows are matched by email (the unique constraint on
`operators.email`) and skipped. `IntegrityError` on the insert path is
swallowed as a defensive guard against concurrent seeders racing on the
same email.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

# Ensure app module is importable from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed(limit: int | None) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.core.database import async_session_maker
    from app.database.models import Operator
    from app.schemas.enums import OperatorStatus
    from app.utils.logging import get_logger

    logger = get_logger(__name__)

    fixtures_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "seed_operators.json"
    )
    rows = json.loads(fixtures_path.read_text())
    if limit is not None:
        rows = rows[:limit]

    logger.info(
        "Starting operator seed",
        extra={"fixture_count": len(rows), "fixture_path": str(fixtures_path)},
    )

    inserted = 0
    skipped = 0

    for i, row in enumerate(rows):
        email = row["email"]
        full_name = row.get("full_name")
        status = row.get("status", OperatorStatus.ACTIVE.value)

        async with async_session_maker() as session:
            existing = await session.execute(
                select(Operator).where(Operator.email == email)
            )
            if existing.scalar_one_or_none() is not None:
                skipped += 1
                logger.info(
                    "Skip operator (already exists)",
                    extra={"index": i, "email": email, "full_name": full_name},
                )
                continue

            try:
                session.add(
                    Operator(
                        id=uuid4(),
                        # supabase_user_id stays null until first login
                        supabase_user_id=None,
                        email=email,
                        full_name=full_name,
                        status=status,
                    )
                )
                await session.commit()
                inserted += 1
                logger.info(
                    "Inserted operator",
                    extra={"index": i, "email": email, "full_name": full_name},
                )
            except IntegrityError as exc:
                # Another seeder won the race on this email. Treat as skip.
                await session.rollback()
                skipped += 1
                logger.warning(
                    "IntegrityError on operator insert — treating as skip",
                    extra={"index": i, "email": email, "error": str(exc)},
                )

    logger.info(
        "Operator seed complete",
        extra={"inserted": inserted, "skipped": skipped, "total": len(rows)},
    )
    print(f"Seeded {inserted} operators ({skipped} skipped)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic operators into Converio")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N rows from the fixture (default: all)",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.limit))
