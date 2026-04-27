#!/usr/bin/env python3
"""Seed synthetic candidates by firing CandidateIndexingWorkflow with structured profile input."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure app module is importable from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed(limit: int, task_queue: str) -> None:
    from temporalio.client import Client

    from app.core.config import settings
    from app.schemas.product.candidate import CandidateIndexingInput, CandidateProfile

    fixtures_path = Path(__file__).parent.parent / "tests" / "fixtures" / "seed_candidates.json"
    profiles = json.loads(fixtures_path.read_text())[:limit]

    client = await Client.connect(
        f"{settings.temporal.host}:{settings.temporal.port}",
        namespace=settings.temporal.namespace,
    )

    print(f"Seeding {len(profiles)} candidates to queue '{task_queue}'...")

    success = 0
    skipped = 0

    for i, profile in enumerate(profiles):
        candidate_profile = CandidateProfile.model_validate(profile)

        workflow_id = f"seed-candidate-{profile['full_name'].lower().replace(' ', '-')}-{i}"

        try:
            # Check if workflow already exists (idempotency)
            inp = CandidateIndexingInput(
                input_kind="profile",
                profile=candidate_profile,
                source="seed",
                source_recruiter_id=None,
            )

            await client.start_workflow(
                "CandidateIndexingWorkflow",
                inp.model_dump(mode="json"),
                id=workflow_id,
                task_queue=task_queue,
            )
            success += 1
            print(f"  [{i+1}/{len(profiles)}] Started: {profile['full_name']}")

        except Exception as e:
            if "already" in str(e).lower():
                skipped += 1
                print(f"  [{i+1}/{len(profiles)}] Skip (already exists): {profile['full_name']}")
            else:
                print(f"  [{i+1}/{len(profiles)}] ERROR {profile['full_name']}: {e}")

        # Small delay to avoid overwhelming the worker
        if (i + 1) % 10 == 0:
            await asyncio.sleep(1)

    print(f"\nDone. Started: {success}, Skipped (idempotent): {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic candidates into Converio")
    parser.add_argument("--limit", type=int, default=100, help="Number of profiles to seed (default: 100)")
    parser.add_argument("--task-queue", default="converio-queue", help="Temporal task queue")
    args = parser.parse_args()
    asyncio.run(seed(args.limit, args.task_queue))
