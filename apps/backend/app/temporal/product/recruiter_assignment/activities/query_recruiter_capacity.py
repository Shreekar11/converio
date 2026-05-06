"""Recruiter Assignment Agent — `query_recruiter_capacity` tool activity.

Returns a per-recruiter capacity snapshot (current open roles vs max capacity)
for a list of recruiter IDs. Backs the `capacity` tool exposed to the LLM
agent during HITL #1 candidate proposal generation.

Design notes
------------
* Single PG session for the whole batch — small N (≤ top_n recruiters per
  call, typically ≤ 10), so issuing 2 round-trips per recruiter is fine and
  keeps the activity simple. Switch to a single grouped query if N grows.
* Unknown / missing recruiter IDs are skipped with a structured warning
  rather than failing the activity. The agent's downstream logic must
  tolerate a shorter `capacity` list than its input — silently dropping
  beats hard-failing the whole assignment workflow on a stale ID.
* `capacity_max` is hard-coded to `DEFAULT_CAPACITY_MAX = 5` because the
  `recruiters` table only carries the boolean `at_capacity` flag — no
  numeric ceiling column. Centralizing the constant here means a future
  schema migration that adds `recruiters.capacity_max` only needs to flip
  the lookup, not chase the literal across activities.
* `at_capacity` returned in the record is *recomputed* from the live open
  role count rather than read off `Recruiter.at_capacity`, so the agent
  always sees a fresh value even if the periodic capacity-flag job is
  behind.
* Only parameterized queries (SQLAlchemy Core / ORM `select`) — no string
  interpolation against user-controlled input.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Assignment, Recruiter
from app.schemas.product.recruiter_assignment import CapacityRecord
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default ceiling until `recruiters.capacity_max` exists on the model.
# Keep in sync with any UI-side capacity slider once that schema lands.
DEFAULT_CAPACITY_MAX: int = 5

# Statuses that count toward a recruiter's "active load" for capacity math.
# `recommended` is excluded — those are AI suggestions awaiting operator
# confirmation and have not actually been assigned yet.
# `rejected` / `declined_by_recruiter` are terminal and do not consume capacity.
ACTIVE_ASSIGNMENT_STATUSES: tuple[str, ...] = ("operator_confirmed", "notified")


@ActivityRegistry.register("recruiter_assignment", "query_recruiter_capacity")
@activity.defn(name="recruiter_assignment.query_recruiter_capacity")
async def query_recruiter_capacity(payload: dict) -> dict:
    """Return current load snapshots for the requested recruiters.

    Payload
    -------
    `recruiter_ids: list[str]` — UUID strings of recruiters to query.

    Returns
    -------
    `{"capacity": [CapacityRecord.model_dump(mode="json"), ...]}`

    Order of the output list mirrors the input order, with unknown /
    invalid IDs filtered out (see module docstring).
    """
    raw_ids: list[str] = payload.get("recruiter_ids", []) or []

    # Validate UUIDs up front so we can skip malformed entries with a
    # structured warning instead of crashing inside the DB layer.
    parsed_ids: list[tuple[str, uuid.UUID]] = []
    for raw_id in raw_ids:
        try:
            parsed_ids.append((raw_id, uuid.UUID(raw_id)))
        except (TypeError, ValueError):
            LOGGER.warning(
                "Skipping invalid recruiter_id in capacity query",
                extra={"recruiter_id": raw_id},
            )

    if not parsed_ids:
        return {"capacity": []}

    capacity_records: list[CapacityRecord] = []

    async with async_session_maker() as session:
        for recruiter_id_str, recruiter_uuid in parsed_ids:
            recruiter_row = await session.execute(
                select(Recruiter.id).where(Recruiter.id == recruiter_uuid)
            )
            if recruiter_row.scalar_one_or_none() is None:
                LOGGER.warning(
                    "Recruiter not found during capacity query — skipping",
                    extra={"recruiter_id": recruiter_id_str},
                )
                continue

            count_result = await session.execute(
                select(func.count(Assignment.id)).where(
                    Assignment.recruiter_id == recruiter_uuid,
                    Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
                )
            )
            open_role_count = int(count_result.scalar_one() or 0)

            capacity_max = DEFAULT_CAPACITY_MAX
            record = CapacityRecord(
                recruiter_id=recruiter_id_str,
                current_open_roles=open_role_count,
                capacity_max=capacity_max,
                at_capacity=open_role_count >= capacity_max,
            )
            capacity_records.append(record)

    LOGGER.info(
        "Capacity query complete",
        extra={
            "requested": len(raw_ids),
            "returned": len(capacity_records),
        },
    )

    return {
        "capacity": [record.model_dump(mode="json") for record in capacity_records]
    }
