"""Activity: summarize_recruiter_track_record.

LLM-driven generation of a short (2-3 sentence) recruiter profile narrative for
the Converio operator HITL review dashboard. Used by the Recruiter Assignment
Agent (Agent 0) to surface a human-readable proof-of-track-record blurb
alongside fit scoring.

Per CLAUDE.md AI/LLM rules:
  * The system prompt contains NO user-controlled content. The user prompt is
    built strictly from internal database rows (`recruiters` + the recruiter's
    own `recruiter_placements`). Recruiters do not author these fields as
    free-form prompt input — they are wizard-validated structured data
    persisted by Converio.
  * LLM exceptions are NOT caught here; they bubble up so the Temporal retry
    policy declared at the workflow call site fires.
  * Output is constrained by the `TrackRecordSummary` Pydantic schema
    (`max_length=500`) so a runaway model cannot produce an unbounded blob.

The activity is deterministic with respect to inputs at the Temporal layer
(the LLM call itself is non-deterministic, which is precisely why it lives in
an activity rather than a workflow).
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.core.llm import LLMMessage, get_llm_client
from app.database.models import Recruiter, RecruiterPlacement
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_PLACEMENT_CONTEXT_LIMIT = 10
_TOP_ROLE_TITLES = 5


class TrackRecordSummary(BaseModel):
    """Constrained LLM output — short narrative for the operator dashboard."""

    narrative: str = Field(..., max_length=500)


_SYSTEM_PROMPT = """You are writing a 2-3 sentence recruiter profile summary for a Converio operator's review dashboard.
Summarize the recruiter's track record concisely. Focus on: domain expertise, company stage experience,
and placement success. Be specific - cite actual numbers when available.
Return JSON matching the TrackRecordSummary schema."""


def _format_list(values: list[str]) -> str:
    """Render a list[str] as a compact comma-separated string for the prompt."""
    if not values:
        return "(none)"
    return ", ".join(values)


def _format_optional(value: object) -> str:
    """Render a possibly-null scalar as a string for the prompt."""
    if value is None:
        return "(unknown)"
    return str(value)


def _build_user_prompt(
    *,
    full_name: str,
    domain_expertise: list[str],
    total_placements: int,
    avg_days_to_close: int | None,
    fill_rate_pct: object,
    recent_role_titles: list[str],
    company_stages: list[str],
) -> str:
    """Assemble the user-role prompt from internal recruiter + placement data.

    All fields here originate from Converio-controlled rows, not free-form
    end-user input. Even so, the content is delivered via the `user` role —
    never inlined into the system prompt — to preserve the trust boundary.
    """
    return (
        f"Recruiter: {full_name}\n"
        f"Domain expertise: {_format_list(domain_expertise)}\n"
        f"Total placements: {total_placements}\n"
        f"Average days to close: {_format_optional(avg_days_to_close)}\n"
        f"Fill rate: {_format_optional(fill_rate_pct)}%\n"
        f"Recent role titles: {_format_list(recent_role_titles)}\n"
        f"Company stages placed at: {_format_list(company_stages)}\n"
    )


@ActivityRegistry.register("recruiter_assignment", "summarize_recruiter_track_record")
@activity.defn(name="recruiter_assignment.summarize_recruiter_track_record")
async def summarize_recruiter_track_record(payload: dict) -> dict:
    """Generate a short LLM-authored narrative for a recruiter's track record.

    Args:
        payload: dict with keys
            - recruiter_id (str): UUID string of the recruiter to summarize.

    Returns:
        dict with keys
            - narrative (str): <=500 char operator-facing summary.
            - recruiter_id (str): Echo of the input recruiter_id.

    Raises:
        ValueError: If `recruiter_id` is missing, malformed, or no recruiter
            row matches.
        Exception: LLM/transport errors are intentionally propagated so the
            workflow-level retry policy can fire.
    """
    raw_recruiter_id = payload.get("recruiter_id")
    if not raw_recruiter_id:
        raise ValueError("payload.recruiter_id is required")

    try:
        recruiter_uuid = uuid.UUID(str(raw_recruiter_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"payload.recruiter_id is not a valid UUID: {raw_recruiter_id!r}"
        ) from exc

    LOGGER.info(
        "Summarizing recruiter track record",
        extra={"recruiter_id": str(recruiter_uuid)},
    )

    # 1) + 2) Fetch recruiter row + top 10 placements (most recent first).
    async with async_session_maker() as session:
        recruiter = (
            await session.execute(
                select(Recruiter).where(Recruiter.id == recruiter_uuid)
            )
        ).scalar_one_or_none()

        if recruiter is None:
            raise ValueError(f"Recruiter {recruiter_uuid} not found")

        placements_result = await session.execute(
            select(RecruiterPlacement)
            .where(RecruiterPlacement.recruiter_id == recruiter_uuid)
            .order_by(RecruiterPlacement.placed_at.desc().nullslast())
            .limit(_PLACEMENT_CONTEXT_LIMIT)
        )
        placements: list[RecruiterPlacement] = list(placements_result.scalars().all())

    # Derive prompt context from placements (top role titles + unique stages).
    recent_role_titles: list[str] = [
        p.role_title for p in placements if p.role_title
    ][:_TOP_ROLE_TITLES]

    seen_stages: set[str] = set()
    company_stages: list[str] = []
    for p in placements:
        stage = p.company_stage
        if stage and stage not in seen_stages:
            seen_stages.add(stage)
            company_stages.append(stage)

    domain_expertise: list[str] = list(recruiter.domain_expertise or [])
    fill_rate_pct = (
        recruiter.fill_rate_pct if recruiter.fill_rate_pct is not None else None
    )

    # 3) Build prompt.
    user_prompt = _build_user_prompt(
        full_name=recruiter.full_name,
        domain_expertise=domain_expertise,
        total_placements=recruiter.total_placements,
        avg_days_to_close=recruiter.avg_days_to_close,
        fill_rate_pct=fill_rate_pct,
        recent_role_titles=recent_role_titles,
        company_stages=company_stages,
    )

    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_prompt),
    ]

    LOGGER.info(
        "Calling LLM for recruiter narrative",
        extra={
            "recruiter_id": str(recruiter_uuid),
            "placement_context_count": len(placements),
            "domain_count": len(domain_expertise),
            "stage_count": len(company_stages),
        },
    )

    # 4) LLM call. Errors intentionally bubble up for Temporal retry.
    llm = get_llm_client()
    summary = await llm.structured_complete(
        messages=messages,
        schema=TrackRecordSummary,
    )

    LOGGER.info(
        "Recruiter narrative generated",
        extra={
            "recruiter_id": str(recruiter_uuid),
            "narrative_len": len(summary.narrative),
        },
    )

    # 5) Return contract.
    return {
        "narrative": summary.narrative,
        "recruiter_id": str(recruiter_uuid),
    }
