"""Recruiter Assignment Agent — `persist_proposal` activity.

Final write step in the Recruiter Assignment workflow before HITL #1: it
freezes the agent's proposed recruiter set into the `operator_proposals`
table so the operator UI can fetch a stable snapshot via SSE/polling.

Design notes
------------
* **Supersede-then-insert ordering.** Any prior unsuperseded proposal for
  this `job_id` is marked superseded *before* the new row is inserted. On
  the operator-rejection re-loop the workflow re-enters this activity with
  a fresh proposal; the older row must be retired so the UI's "latest
  non-superseded proposal" query (`fetch_latest_for_job`) returns the new
  one without ambiguity.
* **Idempotent on Temporal replay.** `supersede_existing` is naturally
  idempotent — once a row's `superseded_at` is set, a re-run sets nothing
  (rowcount=0). The follow-up `create_proposal` then writes a new row.
  Worst case after a partial failure + retry: one extra superseded
  proposal sits in the table, which is acceptable history. The "winner"
  is always the single non-superseded row created by the final successful
  attempt.
* **Two transactions, not one.** `OperatorProposalRepository.supersede_existing`
  and `create_proposal` each commit their own transaction. We accept that
  split here because (a) the supersede is monotonic — re-applying it is a
  no-op — and (b) folding both into a single atomic write would require
  duplicating repo internals or refactoring the repo, which is out of
  scope for this activity. If the create step fails, retries restore
  consistency.
* **No swallowed DB errors.** Any SQLAlchemy/connection failure propagates
  so Temporal's activity retry policy can handle it (transient PG blips,
  pool exhaustion, etc.).

Inputs (dict):
    job_id: str          — UUID of the Job row, as string.
    workflow_id: str     — Temporal workflow id (for proposal provenance).
    proposal: dict       — Full `OperatorProposal.model_dump()` payload.
    quality_flag: str    — "high" | "medium" | "low" aggregate confidence.

Returns:
    {
        "proposal_id":      str,  # UUID of newly inserted proposal row
        "job_id":           str,  # echoed back for callers/log correlation
        "superseded_count": int,  # rows marked superseded by this run
    }
"""
from __future__ import annotations

import uuid

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.operator_proposals import OperatorProposalRepository
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_VALID_QUALITY_FLAGS = {"high", "medium", "low"}


@ActivityRegistry.register("recruiter_assignment", "persist_proposal")
@activity.defn(name="recruiter_assignment.persist_proposal")
async def persist_proposal(payload: dict) -> dict:
    """Supersede any prior proposal and insert the freshly generated one.

    See module docstring for ordering and idempotency contract.
    """
    job_id_raw = payload.get("job_id")
    workflow_id = payload.get("workflow_id")
    proposal = payload.get("proposal")
    quality_flag = payload.get("quality_flag")

    # ── Input validation ────────────────────────────────────────────────
    if not isinstance(job_id_raw, str) or not job_id_raw.strip():
        raise ValueError("persist_proposal: 'job_id' is required (str UUID)")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("persist_proposal: 'workflow_id' is required (str)")
    if not isinstance(proposal, dict):
        raise ValueError("persist_proposal: 'proposal' must be a dict (OperatorProposal.model_dump())")
    if not isinstance(quality_flag, str) or quality_flag not in _VALID_QUALITY_FLAGS:
        raise ValueError(
            f"persist_proposal: 'quality_flag' must be one of {sorted(_VALID_QUALITY_FLAGS)}, "
            f"got {quality_flag!r}"
        )

    try:
        job_uuid = uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"persist_proposal: invalid job_id UUID {job_id_raw!r}"
        ) from exc

    LOGGER.info(
        "Persisting operator proposal",
        extra={
            "job_id": job_id_raw,
            "workflow_id": workflow_id,
            "quality_flag": quality_flag,
        },
    )

    # ── DB writes ───────────────────────────────────────────────────────
    async with async_session_maker() as session:
        repo = OperatorProposalRepository(session)

        # 1. Mark prior unsuperseded proposals as superseded.
        superseded_count = await repo.supersede_existing(job_uuid)

        # 2. Insert the new proposal row.
        new_proposal = await repo.create_proposal(
            job_id=job_uuid,
            workflow_id=workflow_id,
            payload=proposal,
            quality_flag=quality_flag,
        )

    LOGGER.info(
        "Operator proposal persisted",
        extra={
            "job_id": job_id_raw,
            "workflow_id": workflow_id,
            "proposal_id": str(new_proposal.id),
            "superseded_count": superseded_count,
            "quality_flag": quality_flag,
        },
    )

    return {
        "proposal_id": str(new_proposal.id),
        "job_id": job_id_raw,
        "superseded_count": int(superseded_count),
    }
