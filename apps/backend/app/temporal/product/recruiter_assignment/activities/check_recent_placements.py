"""Activity: check_recent_placements.

Returns the most recent placements (up to 10) for a given recruiter within a
configurable lookback window. Used by the Recruiter Assignment Agent (Agent 0)
during fit scoring — surfaces fresh, in-domain proof points to the LLM.

Notes:
  * `since_days` is clamped to [30, 180] to prevent the agent from issuing
    pathological short or wide queries against the placements table.
  * `days_to_close` is intentionally `None`: it's a derived metric (computed in
    `recruiter_indexing.compute_placement_metrics`) and is not stored on the
    `recruiter_placements` row. The PlacementRecord schema permits None.
  * Query is parameterized via SQLAlchemy `bindparams` (no string concat with
    user input) and scoped to the supplied recruiter_id only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from temporalio import activity

from app.core.database import async_session_maker
from app.schemas.product.recruiter_assignment import PlacementRecord
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_SINCE_DAYS_DEFAULT = 90
_SINCE_DAYS_MIN = 30
_SINCE_DAYS_MAX = 180
_RESULT_LIMIT = 10


def _clamp_since_days(value: int | None) -> int:
    """Clamp `since_days` into the supported [30, 180] window."""
    if value is None:
        return _SINCE_DAYS_DEFAULT
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return _SINCE_DAYS_DEFAULT
    if as_int < _SINCE_DAYS_MIN:
        return _SINCE_DAYS_MIN
    if as_int > _SINCE_DAYS_MAX:
        return _SINCE_DAYS_MAX
    return as_int


@ActivityRegistry.register("recruiter_assignment", "check_recent_placements")
@activity.defn(name="recruiter_assignment.check_recent_placements")
async def check_recent_placements(payload: dict) -> dict:
    """Fetch up to 10 most-recent placements for a recruiter within `since_days`.

    Args:
        payload: dict with keys
            - recruiter_id (str): UUID string of the recruiter.
            - since_days (int, optional): Lookback window in days; clamped to
              [30, 180]. Defaults to 90.

    Returns:
        dict with keys
            - placements (list[dict]): PlacementRecord.model_dump(mode="json")
              entries, ordered most-recent-first.
            - count (int): Number of placements returned (<= 10).
            - recruiter_id (str): Echo of the input recruiter_id.

    Raises:
        ValueError: If `recruiter_id` is missing or not a valid UUID.
    """
    raw_recruiter_id = payload.get("recruiter_id")
    if not raw_recruiter_id:
        raise ValueError("payload.recruiter_id is required")

    try:
        recruiter_uuid = uuid.UUID(str(raw_recruiter_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"payload.recruiter_id is not a valid UUID: {raw_recruiter_id!r}"
        ) from exc

    since_days = _clamp_since_days(payload.get("since_days"))
    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    LOGGER.info(
        "Checking recent placements",
        extra={
            "recruiter_id": str(recruiter_uuid),
            "since_days": since_days,
            "cutoff": cutoff.isoformat(),
        },
    )

    # Parameterized query — SQLAlchemy binds prevent injection. Scope is the
    # supplied recruiter only; no cross-recruiter joins.
    stmt = text(
        """
        SELECT id, role_title, company_name, company_stage, placed_at
        FROM recruiter_placements
        WHERE recruiter_id = :recruiter_id
          AND placed_at IS NOT NULL
          AND placed_at >= :cutoff
        ORDER BY placed_at DESC
        LIMIT :limit
        """
    )

    async with async_session_maker() as session:
        result = await session.execute(
            stmt,
            {
                "recruiter_id": recruiter_uuid,
                "cutoff": cutoff,
                "limit": _RESULT_LIMIT,
            },
        )
        rows = result.all()

    placements: list[dict] = []
    for row in rows:
        record = PlacementRecord(
            role_title=row.role_title,
            company_stage=row.company_stage,
            placed_at=row.placed_at.isoformat() if row.placed_at is not None else None,
            # `days_to_close` is a derived metric (see compute_placement_metrics)
            # and is not stored on the placements row.
            days_to_close=None,
        )
        placements.append(record.model_dump(mode="json"))

    LOGGER.info(
        "Recent placements fetched",
        extra={
            "recruiter_id": str(recruiter_uuid),
            "since_days": since_days,
            "count": len(placements),
        },
    )

    return {
        "placements": placements,
        "count": len(placements),
        "recruiter_id": str(recruiter_uuid),
    }
