import uuid

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.recruiter_placements import RecruiterPlacementRepository
from app.repositories.recruiters import RecruiterRepository
from app.schemas.product.recruiter import ComputedMetrics
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("recruiter_indexing", "compute_placement_metrics")
@activity.defn
async def compute_placement_metrics(recruiter_id: str) -> dict:
    """Derive placement-derived metrics for a recruiter.

    v1 scope:
      * `total_placements` — count of `recruiter_placements` rows.
      * `placements_by_stage` — count grouped by `company_stage` (skipping null stages).
      * `fill_rate_pct` / `avg_days_to_close` — pulled from existing recruiter columns
        (seed/wizard pre-set them). Indexing does NOT derive these — assignments table is
        unused at this stage. TODO: derive avg_days_to_close from `placed_at` deltas
        once role-open dates are structured (currently `placed_at` is ISO timestamp only).
    """
    LOGGER.info("Computing placement metrics", extra={"recruiter_id": recruiter_id})

    recruiter_uuid = uuid.UUID(recruiter_id)

    async with async_session_maker() as session:
        recruiter_repo = RecruiterRepository(session)
        placement_repo = RecruiterPlacementRepository(session)

        recruiter = await recruiter_repo.get_by_id(recruiter_uuid)
        if recruiter is None:
            raise ValueError(f"Recruiter {recruiter_id} not found in PG")

        placements = await placement_repo.get_by_recruiter(recruiter_uuid)

        total_placements = len(placements)

        placements_by_stage: dict[str, int] = {}
        for p in placements:
            if p.company_stage is None:
                continue
            placements_by_stage[p.company_stage] = (
                placements_by_stage.get(p.company_stage, 0) + 1
            )

        # Pull existing pre-set values; cast Decimal to float for JSON-serialization across Temporal.
        # TODO: derive avg_days_to_close from `placed_at` deltas once role-open dates are tracked.
        fill_rate_pct = (
            float(recruiter.fill_rate_pct) if recruiter.fill_rate_pct is not None else None
        )
        avg_days_to_close = (
            int(recruiter.avg_days_to_close) if recruiter.avg_days_to_close is not None else None
        )

    metrics = ComputedMetrics(
        fill_rate_pct=fill_rate_pct,
        avg_days_to_close=avg_days_to_close,
        total_placements=total_placements,
        placements_by_stage=placements_by_stage,
    )

    LOGGER.info(
        "Placement metrics computed",
        extra={
            "recruiter_id": recruiter_id,
            "total_placements": total_placements,
            "stages_covered": list(placements_by_stage.keys()),
            "fill_rate_pct": fill_rate_pct,
            "avg_days_to_close": avg_days_to_close,
        },
    )

    return metrics.model_dump(mode="json")
