import hashlib
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Candidate
from app.schemas.product.candidate import CandidateProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def compute_dedup_hash(name: str, email: str | None) -> str:
    """sha256(lower(name) + normalized_email) — must match resolve_entity_duplicates."""
    normalized_name = name.strip().lower()
    normalized_email = re.sub(r"\s+", "", (email or "").strip().lower())
    return hashlib.sha256((normalized_name + normalized_email).encode()).hexdigest()


@ActivityRegistry.register("candidate_indexing", "persist_candidate_record")
@activity.defn
async def persist_candidate_record(
    profile_data: dict,
    embedding: list[float],
    github_signals: dict,
    source: str,
    source_recruiter_id: str | None,
    existing_candidate_id: str | None,
) -> dict:
    profile = CandidateProfile.model_validate(profile_data)
    dedup_hash = compute_dedup_hash(profile.full_name, profile.email)

    skills_json = [s.model_dump(mode="json") for s in profile.skills]
    work_history_json = [w.model_dump(mode="json") for w in profile.work_history]
    education_json = [e.model_dump(mode="json") for e in profile.education]

    async with async_session_maker() as session:
        if existing_candidate_id:
            result = await session.execute(
                select(Candidate).where(Candidate.id == uuid.UUID(existing_candidate_id))
            )
            candidate = result.scalar_one_or_none()
            if candidate is None:
                LOGGER.warning(
                    "existing_candidate_id not found, inserting new",
                    extra={"id": existing_candidate_id},
                )
                existing_candidate_id = None
            else:
                if profile.email and not candidate.email:
                    candidate.email = profile.email
                if profile.github_username and not candidate.github_username:
                    candidate.github_username = profile.github_username
                if profile.seniority:
                    candidate.seniority = profile.seniority
                if profile.years_experience:
                    candidate.years_experience = profile.years_experience
                if profile.location:
                    candidate.location = profile.location
                if profile.stage_fit:
                    candidate.stage_fit = profile.stage_fit
                if skills_json:
                    candidate.skills = skills_json
                if work_history_json:
                    candidate.work_history = work_history_json
                if education_json:
                    candidate.education = education_json
                if github_signals:
                    candidate.github_signals = github_signals
                if profile.resume_text:
                    candidate.resume_text = profile.resume_text
                if embedding:
                    candidate.embedding = embedding
                candidate.updated_at = datetime.now(timezone.utc)
                await session.commit()
                LOGGER.info("Updated existing candidate", extra={"id": existing_candidate_id})
                return {"candidate_id": existing_candidate_id, "was_insert": False}

        candidate = Candidate(
            full_name=profile.full_name,
            email=profile.email,
            phone=profile.phone,
            github_username=profile.github_username,
            linkedin_url=profile.linkedin_url,
            location=profile.location,
            seniority=profile.seniority,
            years_experience=profile.years_experience,
            stage_fit=profile.stage_fit or [],
            skills=skills_json,
            work_history=work_history_json,
            education=education_json,
            github_signals=github_signals or {},
            resume_text=profile.resume_text,
            embedding=embedding if embedding else None,
            source=source,
            source_recruiter_id=uuid.UUID(source_recruiter_id) if source_recruiter_id else None,
            dedup_hash=dedup_hash,
            status="indexing",
            completeness_score=0,
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        candidate_id = str(candidate.id)

    LOGGER.info("Inserted new candidate", extra={"id": candidate_id})
    return {"candidate_id": candidate_id, "was_insert": True}
