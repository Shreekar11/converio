import hashlib
import re

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.candidates import CandidateRepository
from app.schemas.product.candidate import CandidateProfile, ResolveDuplicatesResult
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def compute_dedup_hash(name: str, email: str | None) -> str:
    """Canonical dedup key: sha256(lower(name) + normalized_email)."""
    normalized_name = name.strip().lower()
    normalized_email = re.sub(r"\s+", "", (email or "").strip().lower())
    raw = normalized_name + normalized_email
    return hashlib.sha256(raw.encode()).hexdigest()


@ActivityRegistry.register("candidate_indexing", "resolve_entity_duplicates")
@activity.defn
async def resolve_entity_duplicates(profile_data: dict) -> dict:
    profile = CandidateProfile.model_validate(profile_data)

    dedup_hash = compute_dedup_hash(profile.full_name, profile.email)

    LOGGER.info(
        "Checking for duplicate candidate",
        extra={"name": profile.full_name, "email": profile.email, "hash": dedup_hash[:12]},
    )

    async with async_session_maker() as session:
        repo = CandidateRepository(session)

        # Primary: dedup hash match
        existing = await repo.get_by_dedup_hash(dedup_hash)
        if existing:
            LOGGER.info(
                "Duplicate found via dedup_hash",
                extra={"existing_id": str(existing.id)},
            )
            return ResolveDuplicatesResult(
                is_duplicate=True,
                existing_candidate_id=str(existing.id),
                match_source="dedup_hash",
            ).model_dump(mode="json")

        # Secondary: GitHub username match (if present)
        if profile.github_username:
            existing = await repo.get_by_github_username(profile.github_username)
            if existing:
                LOGGER.info(
                    "Duplicate found via github_username",
                    extra={"existing_id": str(existing.id), "username": profile.github_username},
                )
                return ResolveDuplicatesResult(
                    is_duplicate=True,
                    existing_candidate_id=str(existing.id),
                    match_source="github_username",
                ).model_dump(mode="json")

    LOGGER.info("No duplicate found", extra={"name": profile.full_name})
    return ResolveDuplicatesResult(
        is_duplicate=False,
        existing_candidate_id=None,
        match_source=None,
    ).model_dump(mode="json")
