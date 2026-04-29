import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Recruiter
from app.schemas.product.recruiter import RecruiterProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Credibility weights — must sum to 1.0.
_WEIGHTS = {
    "bio": 0.15,
    "linkedin_url": 0.10,
    "domain_1plus": 0.15,
    "client_1plus": 0.15,
    "placements_3plus": 0.20,
    "workspace_type": 0.10,
    "recruited_funding_stage": 0.10,
    "stages_2plus": 0.05,
}


def _compute_score(profile: RecruiterProfile) -> float:
    score = 0.0
    score += _WEIGHTS["bio"] if profile.bio else 0
    score += _WEIGHTS["linkedin_url"] if profile.linkedin_url else 0
    score += _WEIGHTS["domain_1plus"] if len(profile.domain_expertise) >= 1 else 0
    score += _WEIGHTS["client_1plus"] if len(profile.past_clients) >= 1 else 0
    score += _WEIGHTS["placements_3plus"] if len(profile.past_placements) >= 3 else 0
    score += _WEIGHTS["workspace_type"] if profile.workspace_type is not None else 0
    score += (
        _WEIGHTS["recruited_funding_stage"]
        if profile.recruited_funding_stage is not None
        else 0
    )

    distinct_stages = {
        p.company_stage for p in profile.past_placements if p.company_stage is not None
    }
    score += _WEIGHTS["stages_2plus"] if len(distinct_stages) >= 2 else 0

    return round(score, 2)


@ActivityRegistry.register("recruiter_indexing", "score_recruiter_credibility")
@activity.defn
async def score_recruiter_credibility(
    recruiter_id: str,
    profile_data: dict,
) -> dict:
    """Deterministic weighted credibility score → recruiter status routing.

    `score < 0.5` → `status="pending"` (operator review queue), else `status="active"`.
    PG row already exists + has been written to by `persist_recruiter_record`; this
    activity only updates `status` + merges `extra["credibility_score"]`.

    A missing recruiter row at this stage logs a warning rather than raising — the
    graph + PG record have already been written and the workflow is effectively
    successful; status update is the only side-effect we lose.
    """
    profile = RecruiterProfile.model_validate(profile_data)
    credibility_score = _compute_score(profile)
    review_required = credibility_score < 0.5
    status = "pending" if review_required else "active"

    LOGGER.info(
        "Credibility scored",
        extra={
            "recruiter_id": recruiter_id,
            "score": credibility_score,
            "status": status,
        },
    )

    recruiter_uuid = uuid.UUID(recruiter_id)

    async with async_session_maker() as session:
        result = await session.execute(
            select(Recruiter).where(Recruiter.id == recruiter_uuid)
        )
        recruiter = result.scalar_one_or_none()
        if recruiter is None:
            LOGGER.warning(
                "Recruiter not found for credibility update",
                extra={"recruiter_id": recruiter_id},
            )
        else:
            recruiter.status = status
            existing_extra = dict(recruiter.extra) if recruiter.extra else {}
            existing_extra["credibility_score"] = credibility_score
            recruiter.extra = existing_extra
            recruiter.updated_at = datetime.now(timezone.utc)
            await session.commit()

    return {
        "credibility_score": credibility_score,
        "status": status,
        "review_required": review_required,
    }
