import uuid

from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.recruiters import RecruiterRepository
from app.schemas.product.recruiter import (
    RecruiterProfile,
    ResolveRecruiterDuplicatesResult,
)
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("recruiter_indexing", "resolve_recruiter_duplicates")
@activity.defn
async def resolve_recruiter_duplicates(profile_data: dict) -> dict:
    """Email-based dedup against existing recruiter row.

    Wizard pre-creates the recruiter row before triggering indexing (Decision 4 of plan),
    so this activity is expected to *always* find an existing row. A miss is treated
    as a fail-fast condition — workflow aborts and operator must investigate.
    """
    profile = RecruiterProfile.model_validate(profile_data)

    LOGGER.info(
        "Resolving recruiter duplicate by email",
        extra={
            "recruiter_id": profile.recruiter_id,
            "email": profile.email,
        },
    )

    async with async_session_maker() as session:
        repo = RecruiterRepository(session)
        existing = await repo.get_by_email(profile.email)

        if existing is None:
            # Per Decision 4: workflow upserts, never inserts. Missing row = wizard contract violation.
            raise ValueError(
                f"Recruiter row not found for email={profile.email}; "
                "wizard must pre-create recruiter before indexing"
            )

        existing_id = str(existing.id)

        # Email is unique-constrained at DB level — UUID mismatch should never happen.
        # Defensive log for ops debugging if it does (e.g. wizard bug, manual SQL).
        try:
            wizard_uuid = uuid.UUID(profile.recruiter_id)
        except (ValueError, TypeError):
            wizard_uuid = None

        if wizard_uuid is not None and existing.id != wizard_uuid:
            LOGGER.warning(
                "Email collision: existing recruiter id differs from wizard-supplied id",
                extra={
                    "email": profile.email,
                    "existing_id": existing_id,
                    "wizard_supplied_id": profile.recruiter_id,
                },
            )

        LOGGER.info(
            "Resolved recruiter via email",
            extra={"existing_id": existing_id, "email": profile.email},
        )

        return ResolveRecruiterDuplicatesResult(
            is_duplicate=True,
            existing_recruiter_id=existing_id,
            match_source="email",
        ).model_dump(mode="json")
