#!/usr/bin/env python3
"""Seed synthetic client companies into PG.

Companies are client orgs Converio engages as a managed-recruiting service.
This script inserts a small synthetic set covering all five `CompanyStage`
values so downstream phases (Job intake, recruiter matching) have a
realistic stage distribution to exercise.

Idempotency: companies are de-duplicated by case-insensitive name match.
The schema does not currently carry a unique constraint on `companies.name`
(see `app/database/models.py` — `name` is `String, nullable=False` only),
so we enforce uniqueness at the application layer here. `IntegrityError` on
insert is still caught defensively so concurrent seeders never error out.
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
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from app.core.database import async_session_maker
    from app.database.models import Company
    from app.schemas.enums import CompanyStatus
    from app.utils.logging import get_logger

    logger = get_logger(__name__)

    fixtures_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "seed_companies.json"
    )
    rows = json.loads(fixtures_path.read_text())
    if limit is not None:
        rows = rows[:limit]

    logger.info(
        "Starting company seed",
        extra={"fixture_count": len(rows), "fixture_path": str(fixtures_path)},
    )

    inserted = 0
    skipped = 0

    for i, row in enumerate(rows):
        name = row["name"]

        async with async_session_maker() as session:
            existing = await session.execute(
                select(Company).where(func.lower(Company.name) == name.lower())
            )
            if existing.scalar_one_or_none() is not None:
                skipped += 1
                logger.info(
                    "Skip company (already exists)",
                    extra={"index": i, "name": name, "stage": row.get("stage")},
                )
                continue

            try:
                session.add(
                    Company(
                        id=uuid4(),
                        name=name,
                        stage=row.get("stage"),
                        industry=row.get("industry"),
                        website=row.get("website"),
                        company_size_range=row.get("company_size_range"),
                        founding_year=row.get("founding_year"),
                        hq_location=row.get("hq_location"),
                        description=row.get("description"),
                        status=CompanyStatus.ACTIVE.value,
                    )
                )
                await session.commit()
                inserted += 1
                logger.info(
                    "Inserted company",
                    extra={
                        "index": i,
                        "name": name,
                        "stage": row.get("stage"),
                        "company_size_range": row.get("company_size_range"),
                    },
                )
            except IntegrityError as exc:
                # Defensive: future schema may add a unique constraint on name.
                await session.rollback()
                skipped += 1
                logger.warning(
                    "IntegrityError on company insert — treating as skip",
                    extra={"index": i, "name": name, "error": str(exc)},
                )

    logger.info(
        "Company seed complete",
        extra={"inserted": inserted, "skipped": skipped, "total": len(rows)},
    )
    print(f"Seeded {inserted} companies ({skipped} skipped)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic companies into Converio")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N rows from the fixture (default: all)",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.limit))
