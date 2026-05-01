from temporalio import activity

from app.core.embeddings import embed_text
from app.schemas.product.candidate import CandidateProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("candidate_indexing", "generate_embedding")
@activity.defn
async def generate_embedding(profile_data: dict) -> dict:
    """Generate 384-dim embedding for candidate profile text."""
    profile = CandidateProfile.model_validate(profile_data)

    skills_csv = ", ".join(s.name for s in profile.skills) if profile.skills else ""
    work_summary = (
        " | ".join(f"{w.role_title} at {w.company}" for w in profile.work_history)
        if profile.work_history
        else ""
    )

    text = " | ".join(
        filter(
            None,
            [
                profile.full_name,
                profile.seniority,
                skills_csv,
                work_summary,
                profile.location,
            ],
        )
    )

    LOGGER.info(
        "Generating embedding",
        extra={"candidate": profile.full_name, "text_len": len(text)},
    )

    embedding = await embed_text(text)

    LOGGER.info(
        "Embedding generated",
        extra={"candidate": profile.full_name, "dim": len(embedding)},
    )

    return {"embedding": embedding}
