#!/usr/bin/env python3
"""Seed synthetic recruiters into PG and fire RecruiterIndexingWorkflow.

Differences from `seed_candidates.py`:

- Recruiter rows must exist in PG before the indexing workflow fires
  (workflow is enrichment-only — see Decision 4 in the plan). This script
  inserts `Recruiter` + `RecruiterClient` + `RecruiterPlacement` rows
  synchronously, then dispatches the workflow.
- Idempotent re-runs: existing recruiter (matched by email) is reused;
  child rows are only inserted if absent.
- ALLOW_DUPLICATE workflow id reuse so re-running this script (or
  re-indexing after a wizard mutation) does not error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# Ensure app module is importable from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))


def _slug(name: str) -> str:
    """Lowercase + spaces→hyphens (mirrors seed_candidates.py:36)."""
    return name.lower().replace(" ", "-")


def _parse_placed_at(raw: str | None) -> datetime | None:
    """Parse ISO date / datetime string into datetime; return None on miss."""
    if not raw:
        return None
    try:
        # `fromisoformat` handles both '2025-09-12' and '2025-09-12T00:00:00'.
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def seed(limit: int, task_queue: str) -> None:
    from sqlalchemy import select
    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy

    from app.core.config import settings
    from app.core.database import async_session_maker
    from app.database.models import Recruiter, RecruiterClient, RecruiterPlacement
    from app.schemas.product.recruiter import (
        RecruiterClientItem,
        RecruiterIndexingInput,
        RecruiterPlacementItem,
        RecruiterProfile,
    )

    fixtures_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "seed_recruiters.json"
    )
    profiles = json.loads(fixtures_path.read_text())[:limit]

    client = await Client.connect(
        f"{settings.temporal.host}:{settings.temporal.port}",
        namespace=settings.temporal.namespace,
    )

    print(f"Seeding {len(profiles)} recruiters to queue '{task_queue}'...")

    started = 0
    skipped = 0
    db_inserted = 0
    db_skipped = 0

    for i, profile in enumerate(profiles):
        full_name = profile["full_name"]
        email = profile["email"]
        domains = profile.get("domain_expertise", [])

        # ---- 1. Ensure PG rows exist (idempotent). --------------------------
        recruiter_id: str
        async with async_session_maker() as session:
            existing = await session.execute(
                select(Recruiter).where(Recruiter.email == email)
            )
            recruiter_row = existing.scalar_one_or_none()

            if recruiter_row is None:
                recruiter_row = Recruiter(
                    id=uuid4(),
                    full_name=full_name,
                    email=email,
                    linkedin_url=profile.get("linkedin_url"),
                    bio=profile.get("bio"),
                    domain_expertise=list(domains),
                    workspace_type=profile.get("workspace_type"),
                    recruited_funding_stage=profile.get("recruited_funding_stage"),
                    status="pending",
                    at_capacity=False,
                )
                session.add(recruiter_row)
                await session.flush()  # populate recruiter_row.id

                for client_item in profile.get("past_clients", []):
                    session.add(
                        RecruiterClient(
                            id=uuid4(),
                            recruiter_id=recruiter_row.id,
                            client_company_name=client_item["client_company_name"],
                            description=client_item.get("description"),
                            role_focus=list(client_item.get("role_focus", []) or []),
                        )
                    )

                for placement in profile.get("past_placements", []):
                    session.add(
                        RecruiterPlacement(
                            id=uuid4(),
                            recruiter_id=recruiter_row.id,
                            candidate_name=placement["candidate_name"],
                            company_name=placement["company_name"],
                            company_stage=placement.get("company_stage"),
                            role_title=placement["role_title"],
                            placed_at=_parse_placed_at(placement.get("placed_at")),
                            description=placement.get("description"),
                        )
                    )

                await session.commit()
                db_inserted += 1
            else:
                db_skipped += 1

            recruiter_id = str(recruiter_row.id)

        # ---- 2. Build RecruiterProfile and fire workflow. -------------------
        recruiter_profile = RecruiterProfile(
            recruiter_id=recruiter_id,
            full_name=full_name,
            email=email,
            linkedin_url=profile.get("linkedin_url"),
            bio=profile.get("bio"),
            domain_expertise=domains,
            workspace_type=profile.get("workspace_type"),
            recruited_funding_stage=profile.get("recruited_funding_stage"),
            past_clients=[
                RecruiterClientItem(**c) for c in profile.get("past_clients", [])
            ],
            past_placements=[
                RecruiterPlacementItem(**p) for p in profile.get("past_placements", [])
            ],
        )

        workflow_id = f"seed-recruiter-{_slug(full_name)}-{i}"

        try:
            inp = RecruiterIndexingInput(
                input_kind="profile",
                profile=recruiter_profile,
                source="seed",
            )

            await client.start_workflow(
                "RecruiterIndexingWorkflow",
                inp.model_dump(mode="json"),
                id=workflow_id,
                task_queue=task_queue,
                # ALLOW_DUPLICATE so re-runs / re-indexing after Add Client /
                # Add Placement mutations do not error.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            started += 1
            print(f"  [{i+1}/{len(profiles)}] Started: {full_name}")

        except Exception as e:
            if "already" in str(e).lower():
                skipped += 1
                print(f"  [{i+1}/{len(profiles)}] Skip (already exists): {full_name}")
            else:
                print(f"  [{i+1}/{len(profiles)}] ERROR {full_name}: {e}")

        # Small delay to avoid overwhelming the worker
        if (i + 1) % 10 == 0:
            await asyncio.sleep(1)

    print(
        f"\nDone. Started: {started}, Skipped: {skipped}, "
        f"DB-inserted: {db_inserted}, DB-skipped: {db_skipped}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic recruiters into Converio")
    parser.add_argument(
        "--limit", type=int, default=25, help="Number of recruiters to seed (default: 25)"
    )
    parser.add_argument(
        "--task-queue", default="converio-queue", help="Temporal task queue"
    )
    args = parser.parse_args()
    asyncio.run(seed(args.limit, args.task_queue))
