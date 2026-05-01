import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Recruiter
from app.schemas.product.recruiter import ComputedMetrics
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("recruiter_indexing", "persist_recruiter_record")
@activity.defn
async def persist_recruiter_record(
    recruiter_id: str,
    embedding: list[float],
    metrics_data: dict,
) -> dict:
    """Upsert-only writer onto the existing recruiter row.

    Per Decision 4 of plan: wizard pre-creates the recruiter row synchronously, so
    this activity NEVER inserts. A missing row is a fail-fast condition.

    Update semantics:
      * `fill_rate_pct` / `avg_days_to_close` — only overwrite if metrics carries a
        non-null value (preserve existing pre-set values when metrics derivation
        lacks the data, e.g. seed dataset).
      * `total_placements` — always overwrite (derived count, source of truth).
      * `embedding` — always overwrite (fresh on every run).
      * `extra` — merge dict (don't clobber existing keys); store
        `placements_by_stage` snapshot for downstream Agent 0 fit scoring.
    """
    metrics = ComputedMetrics.model_validate(metrics_data)
    recruiter_uuid = uuid.UUID(recruiter_id)

    async with async_session_maker() as session:
        result = await session.execute(
            select(Recruiter).where(Recruiter.id == recruiter_uuid)
        )
        recruiter = result.scalar_one_or_none()
        if recruiter is None:
            raise ValueError(f"Recruiter {recruiter_id} not found in PG")

        if metrics.fill_rate_pct is not None:
            recruiter.fill_rate_pct = metrics.fill_rate_pct
        if metrics.avg_days_to_close is not None:
            recruiter.avg_days_to_close = metrics.avg_days_to_close

        recruiter.total_placements = metrics.total_placements
        recruiter.embedding = embedding

        existing_extra = dict(recruiter.extra) if recruiter.extra else {}
        existing_extra["placements_by_stage"] = metrics.placements_by_stage
        recruiter.extra = existing_extra

        recruiter.updated_at = datetime.now(timezone.utc)

        await session.commit()

    LOGGER.info(
        "Persisted recruiter record",
        extra={
            "recruiter_id": recruiter_id,
            "total_placements": metrics.total_placements,
            "embedding_dim": len(embedding) if embedding else 0,
        },
    )

    return {"recruiter_id": recruiter_id}
