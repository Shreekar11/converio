"""C1 — `classify_role_type` activity.

LLM-driven extraction of `RoleClassification` from raw intake fields. Output is
the `RoleClassification` Pydantic model whose `field_validator` already
normalizes (lower + strip + dedupe + sort) the skill arrays for replay-safety.

Per CLAUDE.md AI/LLM rules: user content (`title`, `jd_text`, `intake_notes`)
flows through the `user` role with explicit delimiters; the privileged system
prompt never inlines untrusted text.

Retry is declared at the workflow call site (`_LLM_RETRY`); the activity itself
just bubbles unhandled exceptions so Temporal's retry policy fires.
"""
from __future__ import annotations

from temporalio import activity

from app.core.llm import LLMMessage, get_llm_client
from app.schemas.enums import CompanyStage, RemoteOnsite, RoleCategory, Seniority
from app.schemas.product.job import RoleClassification
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _enum_values(enum_cls: type) -> str:
    return "|".join(member.value for member in enum_cls)


_SYSTEM_PROMPT = f"""You classify recruiting role intakes for a managed recruiting service.
Return strict JSON matching the RoleClassification schema. Use ONLY the listed
enum values; if uncertain, pick the closest match.

Enums:
- role_category: {_enum_values(RoleCategory)}
- seniority_level: {_enum_values(Seniority)}
- stage_fit (nullable): {_enum_values(CompanyStage)}
- remote_onsite (nullable): {_enum_values(RemoteOnsite)}

Skill arrays:
- must_have_skills: critical, non-negotiable skills (max 20). Lowercase tokens.
- nice_to_have_skills: pluses but not blockers (max 20). Lowercase tokens.

rationale: 1-3 sentence justification (<= 1000 chars). Do not echo the JD verbatim.
"""


def _build_user_prompt(title: str, jd_text: str, intake_notes: str | None) -> str:
    """Delimited user content. Per CLAUDE.md: user input never enters system role."""
    notes = intake_notes if intake_notes else "(none)"
    return (
        "<<<TITLE>>>\n"
        f"{title}\n"
        "<<<END TITLE>>>\n\n"
        "<<<JOB_DESCRIPTION>>>\n"
        f"{jd_text}\n"
        "<<<END JOB_DESCRIPTION>>>\n\n"
        "<<<INTAKE_NOTES>>>\n"
        f"{notes}\n"
        "<<<END INTAKE_NOTES>>>\n"
    )


@ActivityRegistry.register("job_intake", "classify_role_type")
@activity.defn(name="job_intake.classify_role_type")
async def classify_role_type(payload: dict) -> dict:
    """Extract `RoleClassification` from `(title, jd_text, intake_notes)` via LLM.

    Inputs (dict, validated inline):
        title: str
        jd_text: str
        intake_notes: str | None

    Returns:
        `RoleClassification.model_dump(mode="json")` — skill arrays already
        sorted + deduped + lowercased by the model's field validator.
    """
    title = payload.get("title")
    jd_text = payload.get("jd_text")
    intake_notes = payload.get("intake_notes")

    if not isinstance(title, str) or not title.strip():
        raise ValueError("classify_role_type: 'title' is required and must be non-empty")
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("classify_role_type: 'jd_text' is required and must be non-empty")
    if intake_notes is not None and not isinstance(intake_notes, str):
        raise ValueError("classify_role_type: 'intake_notes' must be a string when provided")

    LOGGER.info(
        "Classifying role type",
        extra={
            "title_len": len(title),
            "jd_text_len": len(jd_text),
            "intake_notes_present": bool(intake_notes),
        },
    )

    llm = get_llm_client()
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=_build_user_prompt(title, jd_text, intake_notes)),
    ]

    try:
        classification = await llm.structured_complete(
            messages=messages,
            schema=RoleClassification,
        )
    except Exception as exc:  # noqa: BLE001 — bubble up for Temporal _LLM_RETRY
        LOGGER.error(
            "classify_role_type LLM call failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
        raise

    LOGGER.info(
        "Role classified",
        extra={
            "role_category": classification.role_category.value,
            "seniority_level": classification.seniority_level.value,
            "must_have_count": len(classification.must_have_skills),
            "nice_to_have_count": len(classification.nice_to_have_skills),
        },
    )

    return classification.model_dump(mode="json")
