#!/usr/bin/env python3
"""Seed 100 synthetic candidates into Converio by firing CandidateIndexingWorkflow per profile."""

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

# Ensure app module is importable from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed(limit: int, task_queue: str) -> None:
    from temporalio.client import Client

    from app.core.config import settings

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
        # Render profile to Markdown text (bypasses docling — seed data is already text)
        resume_md = _profile_to_markdown(profile)
        raw_bytes_b64 = base64.b64encode(resume_md.encode()).decode()

        workflow_id = f"seed-candidate-{profile['full_name'].lower().replace(' ', '-')}-{i}"

        try:
            # Check if workflow already exists (idempotency)
            from temporalio.exceptions import WorkflowAlreadyStartedError

            from app.schemas.product.candidate import CandidateIndexingInput

            inp = CandidateIndexingInput(
                raw_bytes_b64=raw_bytes_b64,
                mime_type="text/markdown",
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


def _profile_to_markdown(profile: dict) -> str:
    """Render a seed profile dict to Markdown resume text."""
    lines = [
        f"# {profile['full_name']}",
        f"**Email:** {profile.get('email', '')}  |  **Location:** {profile.get('location', '')}",
    ]
    if profile.get("github_username"):
        lines.append(f"**GitHub:** github.com/{profile['github_username']}")
    if profile.get("linkedin_url"):
        lines.append(f"**LinkedIn:** {profile['linkedin_url']}")

    lines.append(f"\n**Seniority:** {profile.get('seniority', '')}  |  **Experience:** {profile.get('years_experience', 0)} years")

    if profile.get("skills"):
        skill_names = ", ".join(s["name"] for s in profile["skills"])
        lines.append(f"\n## Skills\n{skill_names}")

    if profile.get("work_history"):
        lines.append("\n## Work History")
        for w in profile["work_history"]:
            end = w.get("end_date") or "Present"
            lines.append(f"- **{w['role_title']}** at {w['company']} ({w.get('start_date', '')} – {end})")

    if profile.get("education"):
        lines.append("\n## Education")
        for e in profile["education"]:
            lines.append(f"- {e.get('degree', '')} in {e.get('field_of_study', '')} — {e['institution']} ({e.get('graduation_year', '')})")

    if profile.get("stage_fit"):
        lines.append(f"\n**Stage fit:** {', '.join(profile['stage_fit'])}")

    if profile.get("resume_text"):
        lines.append(f"\n## Summary\n{profile['resume_text']}")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic candidates into Converio")
    parser.add_argument("--limit", type=int, default=100, help="Number of profiles to seed (default: 100)")
    parser.add_argument("--task-queue", default="converio-queue", help="Temporal task queue")
    args = parser.parse_args()
    asyncio.run(seed(args.limit, args.task_queue))
