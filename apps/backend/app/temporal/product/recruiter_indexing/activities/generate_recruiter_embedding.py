from temporalio import activity

from app.core.embeddings import embed_text
from app.schemas.product.recruiter import RecruiterProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _build_blob(profile: RecruiterProfile) -> str:
    """Pipe-joined recruiter text blob — feeds 384-dim sentence-transformers embedding."""
    parts: list[str | None] = [profile.full_name, profile.bio]

    if profile.workspace_type is not None:
        parts.append(profile.workspace_type.value)

    if profile.recruited_funding_stage is not None:
        parts.append(profile.recruited_funding_stage.value)

    if profile.domain_expertise:
        domains_csv = ", ".join(d.value for d in profile.domain_expertise)
        parts.append(f"domain expertise: {domains_csv}")

    if profile.past_clients:
        client_strs: list[str] = []
        for c in profile.past_clients:
            role_focus_csv = ", ".join(c.role_focus) if c.role_focus else ""
            client_strs.append(f"{c.client_company_name} ({role_focus_csv})")
        parts.append(f"past clients: {' ; '.join(client_strs)}")

    if profile.past_placements:
        placement_strs: list[str] = []
        for p in profile.past_placements:
            stage_suffix = f" ({p.company_stage.value})" if p.company_stage else ""
            placement_strs.append(f"{p.role_title} at {p.company_name}{stage_suffix}")
        parts.append(f"past placements: {' ; '.join(placement_strs)}")

    return " | ".join(filter(None, parts))


@ActivityRegistry.register("recruiter_indexing", "generate_recruiter_embedding")
@activity.defn
async def generate_recruiter_embedding(profile_data: dict) -> dict:
    """Generate 384-dim embedding for recruiter profile text blob."""
    profile = RecruiterProfile.model_validate(profile_data)

    text = _build_blob(profile)

    LOGGER.info(
        "Generating recruiter embedding",
        extra={"recruiter": profile.full_name, "text_len": len(text)},
    )

    embedding = await embed_text(text)

    LOGGER.info(
        "Recruiter embedding generated",
        extra={"recruiter": profile.full_name, "dim": len(embedding)},
    )

    return {"embedding": embedding}
