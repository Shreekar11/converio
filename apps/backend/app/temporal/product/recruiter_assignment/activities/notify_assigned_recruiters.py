"""Stub notification activity for PoW. Writes to the notifications table and logs. Real Slack/email integration is post-PoW."""
from __future__ import annotations

import uuid

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.notifications import NotificationRepository
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Channel discriminator for stub notifications. When the real Slack/email
# integration lands post-PoW, replace this with the resolved per-recruiter
# channel and route through the appropriate transport adapter.
_STUB_CHANNEL: str = "stub"


@ActivityRegistry.register("recruiter_assignment", "notify_assigned_recruiters")
@activity.defn(name="recruiter_assignment.notify_assigned_recruiters")
async def notify_assigned_recruiters(payload: dict) -> dict:
    """Persist a stub notification row per assigned recruiter.

    This is a PoW placeholder for the real notification fan-out. It writes
    one row to the `notifications` table per recruiter (channel="stub") and
    emits a structured log line. No outbound HTTP is performed — Slack /
    email transports are intentionally deferred until post-PoW.

    Payload
    -------
    - `recruiter_ids` (list[str]): assigned recruiter UUIDs.
    - `job_id` (str): UUID of the role being assigned.
    - `job_title` (str | None): optional human-friendly title for the
      notification body.

    Returns
    -------
    dict with:
        - `notified_count` (int): number of notifications persisted.
        - `notification_ids` (list[str]): UUIDs of the newly created rows,
          in the same order as the input `recruiter_ids` (skipping any
          malformed entries).

    Behavior
    --------
    - Malformed UUIDs in `recruiter_ids` or `job_id` are logged and skipped
      (recruiters) or raised (job_id) — a missing/invalid job_id makes the
      whole activity meaningless, so we fail loudly.
    - Each notification is written via `NotificationRepository.create_notification`,
      which commits per row. This keeps a partial failure from rolling back
      already-sent notifications, matching the eventual real-transport
      semantics where each Slack/email send is an independent side effect.
    """
    raw_recruiter_ids: list[str] = payload.get("recruiter_ids", []) or []
    raw_job_id = payload.get("job_id")
    job_title: str | None = payload.get("job_title")

    if not raw_job_id:
        raise ValueError("payload.job_id is required for notify_assigned_recruiters")

    try:
        job_uuid = uuid.UUID(str(raw_job_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"payload.job_id must be a valid UUID; got {raw_job_id!r}"
        ) from exc

    # Validate recruiter UUIDs up front so we can skip malformed ones with a
    # structured warning rather than blowing up mid-loop after some rows
    # have already been committed.
    parsed_recruiter_ids: list[tuple[str, uuid.UUID]] = []
    for raw_id in raw_recruiter_ids:
        try:
            parsed_recruiter_ids.append((raw_id, uuid.UUID(str(raw_id))))
        except (TypeError, ValueError):
            LOGGER.warning(
                "Skipping invalid recruiter_id in notify_assigned_recruiters",
                extra={"recruiter_id": raw_id, "job_id": str(job_uuid)},
            )

    if not parsed_recruiter_ids:
        LOGGER.info(
            "No valid recruiter_ids to notify",
            extra={"job_id": str(job_uuid)},
        )
        return {"notified_count": 0, "notification_ids": []}

    display_target = job_title or str(job_uuid)
    notification_ids: list[str] = []

    async with async_session_maker() as session:
        repo = NotificationRepository(session)
        for recruiter_id_str, recruiter_uuid in parsed_recruiter_ids:
            notification_payload = {
                "channel": _STUB_CHANNEL,
                "job_id": str(job_uuid),
                "job_title": job_title,
                "message": f"You have been assigned to role: {display_target}",
            }

            notif = await repo.create_notification(
                recruiter_id=recruiter_uuid,
                job_id=job_uuid,
                payload=notification_payload,
                channel=_STUB_CHANNEL,
            )
            notification_ids.append(str(notif.id))

            LOGGER.info(
                "Recruiter notified (stub)",
                extra={
                    "recruiter_id": recruiter_id_str,
                    "job_id": str(job_uuid),
                },
            )

    LOGGER.info(
        "notify_assigned_recruiters complete",
        extra={
            "job_id": str(job_uuid),
            "requested": len(raw_recruiter_ids),
            "notified_count": len(notification_ids),
        },
    )

    return {
        "notified_count": len(notification_ids),
        "notification_ids": notification_ids,
    }
