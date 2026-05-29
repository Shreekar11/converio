"""scorecard.get_candidate_profile — load the enriched candidate profile.

Read-only DB activity. Hydrates the workflow with the full enriched
profile the upstream Candidate Indexing workflow (Agent 2) persisted:
identity (name, email, github_username), enriched skills, work history,
education, GitHub signals, and the raw resume text used for downstream
citation resolution.

Why this is its own activity (rather than inline workflow code):

* Workflows cannot do I/O. Loading from Postgres MUST happen in an
  activity so Temporal's event history records the exact bytes that
  fed the scoring prompt — on replay the workflow reads the recorded
  output, not the database.
* Schema validation at the workflow boundary. The activity returns a
  pydantic-validated `GetCandidateProfileOutput`; if the upstream
  Candidate model drifts, the failure surfaces here as a clean
  ValidationError instead of as a cryptic prompt-assembly error two
  activities later.
* Composability with the citation resolver. `resolve_citations`
  expects a single `candidate_profile_text` string. We assemble that
  string here (resume_text + skills summary + work-history bullets +
  github signals) so the resolver runs over the exact same text the
  scoring LLM was shown.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.candidates import CandidateRepository
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Hard cap on profile_text size. Citation resolver tier 1 chunks at
# 200-char windows; profiles above this cap blow the chunk count past
# what we can afford to fuzzy-match against in a single activity tick.
# Anything beyond the cap is appended to `enriched_data` for downstream
# inspection but excluded from the inline profile_text used for scoring.
_PROFILE_TEXT_CHAR_CAP = 60_000


class GetCandidateProfileInput(BaseModel):
    """Activity input.

    `candidate_id` arrives as a string because Temporal serializes via
    JSON and UUID round-tripping through `Decimal`/`str` discipline is
    safer than relying on pydantic's UUID coercion at the activity
    boundary (the workflow has already validated it as a UUID upstream).
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(..., description="Candidate UUID as string.")


class GetCandidateProfileOutput(BaseModel):
    """Activity output.

    `profile_text` is the LLM-facing flat string assembled from resume
    text + skills + work history + github signals. The scoring activity
    feeds this verbatim to the Pro-tier LLM. `enriched_data` carries
    structured fields the LLM can read back through `build_scoring_prompt`'s
    formatters (so the same data appears once as flat text for citation
    resolution and once as structured JSON for scoring).
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    full_name: str
    email: str | None = None
    github_username: str | None = None
    profile_text: str = Field(
        ...,
        description=(
            "Assembled flat-text profile (resume + enrichment). Used both "
            "as candidate_profile_text by resolve_citations and as raw "
            "text by build_scoring_prompt."
        ),
    )
    skills: list[str] = Field(default_factory=list)
    years_experience: int | None = None
    enriched_data: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured enrichment fields the scoring prompt formatters "
            "consume verbatim: skills (raw), work_history, education, "
            "github_signals, seniority, stage_fit, location."
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _flatten_skills(raw: Any) -> list[str]:
    """Coerce the Candidate.skills JSONB column into a list[str].

    The column may hold either (a) list of dicts with a `name` field
    (CandidateProfile schema from Agent 2), (b) list of strings, or
    (c) None. We tolerate all three shapes; missing names are silently
    dropped. The output is order-preserving so deterministic prompts
    stay deterministic across replays.
    """
    if not raw or not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _format_work_history_lines(work_history: Any) -> list[str]:
    """Render work history as a list of one-line strings.

    Truncates the list to the 6 most-recent roles (assumed already in
    reverse-chrono order from Agent 2). Older roles inflate the LLM
    prompt without improving scoring fidelity.
    """
    if not work_history or not isinstance(work_history, list):
        return []
    lines: list[str] = []
    for w in work_history[:6]:
        if not isinstance(w, dict):
            continue
        role = (w.get("role") or w.get("title") or "unknown").strip() or "unknown"
        company = (w.get("company") or "unknown").strip() or "unknown"
        start = (w.get("start") or w.get("start_date") or "unknown").strip() or "unknown"
        end_raw = w.get("end") or w.get("end_date") or "present"
        end = end_raw.strip() if isinstance(end_raw, str) else "present"
        lines.append(f"- {role} @ {company} ({start} -> {end})")
    return lines


def _format_github_signals_lines(github_signals: Any) -> list[str]:
    """Render the GitHub signals JSONB dict as key: value lines."""
    if not github_signals or not isinstance(github_signals, dict):
        return []
    lines: list[str] = []
    for k, v in github_signals.items():
        if v is None or v == "" or v == []:
            continue
        # Serialize complex values (dicts/lists) as compact JSON so the
        # profile_text remains a flat string the citation resolver can
        # chunk uniformly.
        if isinstance(v, (dict, list)):
            try:
                v_str = json.dumps(v, sort_keys=True, default=str)
            except (TypeError, ValueError):
                v_str = str(v)
        else:
            v_str = str(v)
        lines.append(f"- {k}: {v_str}")
    return lines


def _assemble_profile_text(
    *,
    full_name: str,
    seniority: str | None,
    years_experience: int | None,
    location: str | None,
    github_username: str | None,
    resume_text: str | None,
    skills: list[str],
    work_history_lines: list[str],
    github_signals_lines: list[str],
) -> str:
    """Build the flat profile_text string.

    Sections are delimited by `=== HEADER ===` markers so the citation
    resolver's chunk windows align with semantic boundaries when
    possible. We do NOT include intake_notes or JD here — those are
    role-specific and `build_scoring_prompt` injects them separately.
    """
    sections: list[str] = []

    summary_line = (
        f"{full_name} — "
        f"{seniority or 'unknown'} engineer, "
        f"{years_experience if years_experience is not None else 'unknown'} years experience, "
        f"based in {location or 'unknown'}. "
        f"GitHub: {github_username or 'unknown'}."
    )
    sections.append("=== CANDIDATE SUMMARY ===\n" + summary_line)

    if skills:
        sections.append("=== SKILLS ===\n" + ", ".join(skills))

    if work_history_lines:
        sections.append("=== WORK HISTORY ===\n" + "\n".join(work_history_lines))

    if github_signals_lines:
        sections.append("=== GITHUB SIGNALS ===\n" + "\n".join(github_signals_lines))

    if resume_text and resume_text.strip():
        sections.append("=== RESUME TEXT ===\n" + resume_text.strip())

    text = "\n\n".join(sections)
    if len(text) > _PROFILE_TEXT_CHAR_CAP:
        text = text[:_PROFILE_TEXT_CHAR_CAP] + "\n…[truncated]"
    return text


@ActivityRegistry.register("scorecard", "get_candidate_profile")
@activity.defn(name="scorecard.get_candidate_profile")
async def get_candidate_profile(payload: dict) -> dict:
    """Load the enriched candidate profile from Postgres.

    Args:
        payload: dict matching `GetCandidateProfileInput`.

    Returns:
        `GetCandidateProfileOutput.model_dump(mode="json")`.

    Raises:
        pydantic.ValidationError: on malformed payload.
        ValueError: if `candidate_id` is not a valid UUID, or if no
            candidate exists with that id (workflow precondition
            violated — fail loud so the workflow can surface a
            structured ApplicationError).
        SQLAlchemyError: on database transport failures, propagated for
            Temporal's activity retry policy.
    """
    model = GetCandidateProfileInput.model_validate(payload)

    try:
        candidate_uuid = UUID(model.candidate_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"get_candidate_profile: candidate_id is not a valid UUID: {model.candidate_id!r}"
        ) from exc

    LOGGER.info(
        "scorecard.get_candidate_profile: loading",
        extra={"candidate_id": model.candidate_id},
    )

    async with async_session_maker() as session:
        repo = CandidateRepository(session)
        candidate = await repo.get_by_id(candidate_uuid)

    if candidate is None:
        raise ValueError(
            f"get_candidate_profile: candidate not found for id={model.candidate_id}"
        )

    skills_list = _flatten_skills(candidate.skills)
    work_history_lines = _format_work_history_lines(candidate.work_history)
    github_signals_lines = _format_github_signals_lines(candidate.github_signals)

    profile_text = _assemble_profile_text(
        full_name=candidate.full_name,
        seniority=candidate.seniority,
        years_experience=candidate.years_experience,
        location=candidate.location,
        github_username=candidate.github_username,
        resume_text=candidate.resume_text,
        skills=skills_list,
        work_history_lines=work_history_lines,
        github_signals_lines=github_signals_lines,
    )

    # Structured enrichment passed through verbatim for the scoring
    # prompt formatters; uses the same field names build_scoring_prompt
    # already reads via `_format_skills` / `_format_work_history` /
    # `_format_github_signals`.
    enriched_data: dict[str, Any] = {
        "seniority": candidate.seniority,
        "stage_fit": candidate.stage_fit or [],
        "location": candidate.location,
        "skills": candidate.skills or [],
        "work_history": candidate.work_history or [],
        "education": candidate.education or [],
        "github_signals": candidate.github_signals or {},
        "resume_text": candidate.resume_text or "",
        "full_name": candidate.full_name,
        "github_username": candidate.github_username,
        "years_experience": candidate.years_experience,
    }

    output = GetCandidateProfileOutput(
        candidate_id=str(candidate.id),
        full_name=candidate.full_name,
        email=candidate.email,
        github_username=candidate.github_username,
        profile_text=profile_text,
        skills=skills_list,
        years_experience=candidate.years_experience,
        enriched_data=enriched_data,
    )

    LOGGER.info(
        "scorecard.get_candidate_profile: loaded",
        extra={
            "candidate_id": str(candidate.id),
            "profile_text_chars": len(profile_text),
            "skills_count": len(skills_list),
            "has_github": bool(candidate.github_username),
            "has_resume": bool(candidate.resume_text),
        },
    )

    return output.model_dump(mode="json")
