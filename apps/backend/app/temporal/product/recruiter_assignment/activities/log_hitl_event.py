"""Recruiter Assignment Agent — `log_hitl_event` activity.

Audit-log writer for the operator HITL #1 decision (operator_approval signal
on the Recruiter Assignment workflow). Persists one row in `hitl_events`
per signal received so we have an immutable trail of who approved, rejected,
or adjusted a recruiter proposal — keyed by job + workflow_id for
cross-referencing with `operator_proposals` and Temporal history.

Design notes
------------
* **Direct SQLAlchemy insert (no repo `create`).** `HitlEventRepository`
  exposes only read paths today (`get_by_job`). Rather than expanding the
  repo surface for a single-call writer that does no aggregation, we issue
  the insert inline. Schema is owned by the model — adding a `create`
  helper here would be premature factoring.
* **No swallowed DB errors.** Any SQLAlchemy/connection failure propagates
  so Temporal's activity retry policy can handle transient PG blips, pool
  exhaustion, etc. The workflow is responsible for retry semantics.
* **Idempotency.** Each invocation creates a new row (UUID PK auto-generated).
  Temporal activity retries after a successful commit but lost ack would
  produce a duplicate audit row; that is acceptable and conservative for
  an append-only audit log — we'd rather over-record than lose a signal.
* **`payload` is opaque.** The raw signal dict (e.g. confirmed_recruiter_ids,
  notes, rejection reason, adjustment diffs) is stored verbatim in JSONB.
  The activity does not validate or reshape it; the workflow is the
  source of truth for signal schema.

Inputs (dict):
    job_id: str          — UUID of the Job row, as string.
    workflow_id: str     — Temporal workflow id that received the signal.
    signal_type: str     — e.g. "operator_approval".
    actor_type: str      — e.g. "operator".
    actor_id: str        — UUID of the operator/company_user, as string.
    action: str          — "approve" | "reject" | "adjust" (free-form here;
                           workflow enforces the enum).
    event_payload: dict | None — raw signal payload stored as JSONB.

Returns:
    {
        "hitl_event_id": str,  # UUID of the newly inserted hitl_events row
        "job_id":        str,  # echoed back for log correlation
        "action":        str,  # echoed back for log correlation
    }
"""
from __future__ import annotations

import uuid

from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import HitlEvent
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_REQUIRED_STR_FIELDS = (
    "job_id",
    "workflow_id",
    "signal_type",
    "actor_type",
    "actor_id",
    "action",
)


@ActivityRegistry.register("recruiter_assignment", "log_hitl_event")
@activity.defn(name="recruiter_assignment.log_hitl_event")
async def log_hitl_event(payload: dict) -> dict:
    """Insert a single audit row into `hitl_events`.

    See module docstring for design contract and idempotency semantics.
    """
    # ── Input validation ────────────────────────────────────────────────
    for field in _REQUIRED_STR_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"log_hitl_event: {field!r} is required (non-empty str)"
            )

    job_id_raw: str = payload["job_id"]
    workflow_id: str = payload["workflow_id"]
    signal_type: str = payload["signal_type"]
    actor_type: str = payload["actor_type"]
    actor_id_raw: str = payload["actor_id"]
    action: str = payload["action"]
    event_payload = payload.get("event_payload")

    if event_payload is not None and not isinstance(event_payload, dict):
        raise ValueError(
            "log_hitl_event: 'event_payload' must be a dict or None, "
            f"got {type(event_payload).__name__}"
        )

    try:
        job_uuid = uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"log_hitl_event: invalid job_id UUID {job_id_raw!r}"
        ) from exc

    try:
        actor_uuid = uuid.UUID(actor_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"log_hitl_event: invalid actor_id UUID {actor_id_raw!r}"
        ) from exc

    LOGGER.info(
        "Logging HITL event",
        extra={
            "job_id": job_id_raw,
            "workflow_id": workflow_id,
            "signal_type": signal_type,
            "actor_type": actor_type,
            "action": action,
        },
    )

    # ── DB write ────────────────────────────────────────────────────────
    # Direct insert: HitlEventRepository has no `create` (read-only repo).
    # DB errors propagate to Temporal for retry.
    async with async_session_maker() as session:
        event = HitlEvent(
            job_id=job_uuid,
            signal_type=signal_type,
            actor_type=actor_type,
            actor_id=actor_uuid,
            action=action,
            payload=event_payload,
            workflow_id=workflow_id,
        )
        session.add(event)
        await session.flush()
        await session.commit()
        event_id = str(event.id)

    LOGGER.info(
        "HITL event persisted",
        extra={
            "job_id": job_id_raw,
            "workflow_id": workflow_id,
            "hitl_event_id": event_id,
            "action": action,
        },
    )

    return {
        "hitl_event_id": event_id,
        "job_id": job_id_raw,
        "action": action,
    }
