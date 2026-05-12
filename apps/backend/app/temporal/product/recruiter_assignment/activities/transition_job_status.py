"""W3.5 — `transition_job_status` activity.

Optimistic, single-transaction status transition for the `Job` row owned by
the Recruiter Assignment workflow. The activity performs a three-step
sequence inside one PG transaction:

  1. Fetch Job by id.
  2. Verify `job.status == from_status` (optimistic concurrency check). If
     the row is already at `to_status` we treat the call as a successful
     no-op — Temporal will retry/replay this activity, and a partial commit
     from a previous attempt must not poison the workflow.
  3. UPDATE Job — assign `to_status` + bump `updated_at`. Commit.

Idempotency contract (Temporal-friendly):
  - First attempt on `from_status`           → transitions, returns delta.
  - Replay/retry after success (`to_status`) → no-op success, returns delta
                                               with `previous_status == to_status`.
  - Any other status                          → ValueError (stale state).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.jobs import JobRepository
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("recruiter_assignment", "transition_job_status")
@activity.defn(name="recruiter_assignment.transition_job_status")
async def transition_job_status(payload: dict) -> dict:
    """Transition `Job.status` from `from_status` -> `to_status`.

    Inputs (dict):
        job_id: str (UUID).
        from_status: str — expected current status (optimistic check).
        to_status: str — target status.

    Returns:
        {"job_id": str, "previous_status": str, "new_status": str}.

    Raises:
        ValueError: payload validation failure, or status mismatch (stale state).
        RuntimeError: Job row not found (caller invariant violation).
    """
    job_id_raw = payload.get("job_id")
    from_status = payload.get("from_status")
    to_status = payload.get("to_status")

    if not isinstance(job_id_raw, str) or not job_id_raw.strip():
        raise ValueError("transition_job_status: 'job_id' is required (str UUID)")
    if not isinstance(from_status, str) or not from_status.strip():
        raise ValueError("transition_job_status: 'from_status' is required (str)")
    if not isinstance(to_status, str) or not to_status.strip():
        raise ValueError("transition_job_status: 'to_status' is required (str)")

    try:
        job_uuid = uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"transition_job_status: invalid job_id UUID {job_id_raw!r}"
        ) from exc

    LOGGER.info(
        "Transitioning job status",
        extra={
            "job_id": job_id_raw,
            "from_status": from_status,
            "to_status": to_status,
        },
    )

    async with async_session_maker() as session:
        job_repo = JobRepository(session)

        job = await job_repo.get_by_id(job_uuid)
        if job is None:
            raise RuntimeError(
                f"transition_job_status: Job {job_id_raw} not found"
            )

        current_status = job.status

        # Replay/retry idempotency: a previous attempt may have already
        # committed the transition. Treat as a successful no-op so the
        # workflow can move forward.
        if current_status == to_status:
            LOGGER.info(
                "Job already at target status — no-op (idempotent replay)",
                extra={
                    "job_id": job_id_raw,
                    "status": current_status,
                },
            )
            return {
                "job_id": job_id_raw,
                "previous_status": current_status,
                "new_status": current_status,
            }

        # Strict optimistic check — anything other than the expected
        # `from_status` (or the already-committed `to_status` above) means
        # the workflow's view of the world is stale.
        if current_status != from_status:
            raise ValueError(
                f"Job {job_id_raw} status is {current_status!r}, "
                f"expected {from_status!r} — stale state"
            )

        job.status = to_status
        job.updated_at = datetime.now(timezone.utc)

        await session.flush()
        await session.commit()

    LOGGER.info(
        "Job status transitioned",
        extra={
            "job_id": job_id_raw,
            "previous_status": from_status,
            "new_status": to_status,
        },
    )

    return {
        "job_id": job_id_raw,
        "previous_status": from_status,
        "new_status": to_status,
    }
