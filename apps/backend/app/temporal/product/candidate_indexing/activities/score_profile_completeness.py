import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Candidate
from app.schemas.product.candidate import CandidateProfile, GitHubSignals
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Completeness weights — must sum to 1.0
_WEIGHTS = {
    "name": 0.05,
    "email": 0.05,
    "seniority": 0.10,
    "years_experience": 0.05,
    "skills_3plus": 0.15,
    "work_history_1plus": 0.15,
    "education": 0.05,
    "github": 0.20,
    "resume_text": 0.10,
    "location": 0.05,
    "stage_fit": 0.05,
}


def _compute_score(profile: CandidateProfile, github: GitHubSignals) -> float:
    score = 0.0
    score += _WEIGHTS["name"] if profile.full_name else 0
    score += _WEIGHTS["email"] if profile.email else 0
    score += _WEIGHTS["seniority"] if profile.seniority else 0
    score += _WEIGHTS["years_experience"] if profile.years_experience else 0
    score += _WEIGHTS["skills_3plus"] if len(profile.skills) >= 3 else 0
    score += _WEIGHTS["work_history_1plus"] if len(profile.work_history) >= 1 else 0
    score += _WEIGHTS["education"] if profile.education else 0
    score += _WEIGHTS["github"] if (profile.github_username and not github.is_empty()) else 0
    score += _WEIGHTS["resume_text"] if profile.resume_text else 0
    score += _WEIGHTS["location"] if profile.location else 0
    score += _WEIGHTS["stage_fit"] if profile.stage_fit else 0
    return round(score, 2)


@ActivityRegistry.register("candidate_indexing", "score_profile_completeness")
@activity.defn
async def score_profile_completeness(
    candidate_id: str,
    profile_data: dict,
    github_signals_data: dict,
) -> dict:
    profile = CandidateProfile.model_validate(profile_data)
    github = GitHubSignals.model_validate(github_signals_data) if github_signals_data else GitHubSignals()

    completeness_score = _compute_score(profile, github)
    review_required = completeness_score < 0.5
    status = "review_queue" if review_required else "indexed"

    LOGGER.info(
        "Completeness scored",
        extra={
            "candidate_id": candidate_id,
            "score": completeness_score,
            "status": status,
        },
    )

    # Update PG row status + completeness_score
    async with async_session_maker() as session:
        result = await session.execute(
            select(Candidate).where(Candidate.id == uuid.UUID(candidate_id))
        )
        candidate = result.scalar_one_or_none()
        if candidate:
            candidate.completeness_score = completeness_score
            candidate.status = status
            candidate.updated_at = datetime.now(timezone.utc)
            await session.commit()
        else:
            LOGGER.warning("Candidate not found for completeness update", extra={"candidate_id": candidate_id})

    return {
        "completeness_score": completeness_score,
        "status": status,
        "review_required": review_required,
    }
