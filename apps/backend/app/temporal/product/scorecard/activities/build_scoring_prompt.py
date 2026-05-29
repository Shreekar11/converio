"""scorecard.build_scoring_prompt — deterministic prompt assembly.

Pure string assembly. No LLM call, no network, no DB. Safe to retry
indefinitely; the output is a function of the input alone.

Why this lives in its own activity (rather than inline in the workflow):

1. **Temporal replay determinism.** The workflow records the activity
   result in event history. On replay, the prompt is read back verbatim
   from history rather than re-assembled — so prompt-template tweaks in
   future deploys do not corrupt in-flight workflows.
2. **Observability.** The assembled prompt is captured as an activity
   payload in the Temporal Web UI and in Langfuse traces, which is the
   single source of truth for "what did we ask the LLM?" debugging.
3. **Cost-tracking friendliness.** Token estimation happens against the
   recorded prompt, not a re-assembled one.

The prompt instructs the LLM to emit a `ScorecardOutput` JSON document
(per `app.schemas.product.scorecard.ScorecardOutput`). The workflow then
computes `overall_match_score` deterministically from per-dimension
scores — the LLM never produces the canonical numeric outcome itself.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Hard caps protect Temporal payload limits (2 MiB default) and keep
# prompt tokens bounded so cost is predictable per scoring call. These
# are deliberately generous — well above expected real-world inputs —
# so the truncation path is an exception, not the norm.
_JD_CHAR_CAP = 16_000
_INTAKE_NOTES_CHAR_CAP = 8_000
_RESUME_CHAR_CAP = 24_000
_RATIONALE_CHAR_CAP = 600
_SUMMARY_CHAR_CAP = 800
_TRUNCATION_MARKER = "\n…[truncated]"


class BuildScoringPromptInput(BaseModel):
    """Input for prompt assembly.

    `candidate_profile_json` is the enriched profile produced by the
    Candidate Indexing workflow (Agent 2). It includes parsed resume
    text, GitHub signals, skills, work history, and education. We accept
    it as a free-form dict (rather than a strict Pydantic model) because
    profile schema evolves on a separate cadence from scoring; missing
    keys are tolerated and noted in the prompt.

    `rubric_json` mirrors the rubric produced by Agent 1. Required keys
    per dimension: `name`, `weight`. Optional: `description`,
    `score_anchors`, `evidence_hints`. Unknown keys are ignored.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_profile_json: dict[str, Any] = Field(
        ..., description="Enriched candidate profile dict (Agent 2 output)."
    )
    job_description: str = Field(..., min_length=1)
    intake_notes: str | None = Field(
        default=None,
        description="Operator's onboarding-call notes for the role; may be empty.",
    )
    rubric_json: dict[str, Any] = Field(
        ...,
        description="Rubric with `dimensions: [{name, weight, description?, ...}]`.",
    )


class BuildScoringPromptOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., description="Fully assembled scoring prompt.")
    candidate_summary: str = Field(
        ...,
        description="One-paragraph candidate summary, suitable for log/trace correlation.",
    )


# ────────────────────────────────────────────────────────────────────────
# Internal helpers — kept private so the assembly contract stays inside
# this module. All helpers are pure functions; do not introduce hidden
# state (caches, RNG, time-of-day) without revisiting determinism.
# ────────────────────────────────────────────────────────────────────────


def _truncate(text: str | None, cap: int) -> str:
    """Return `text` truncated to `cap` characters with a marker suffix.

    The marker is appended *outside* the cap so the visible-text budget
    stays exact; downstream token estimation can budget against the cap
    and treat the marker as overhead.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + _TRUNCATION_MARKER


def _safe_str(value: Any) -> str:
    """Coerce a profile field to a non-empty string or 'unknown'.

    Used throughout candidate summary assembly so a missing field never
    breaks the prompt. The literal 'unknown' is preferred over an empty
    string so the LLM sees that the field was *checked and missing*,
    not silently omitted.
    """
    if value is None:
        return "unknown"
    s = str(value).strip()
    return s if s else "unknown"


def _format_skills(skills: Any) -> str:
    """Render the skills list as a comma-separated string.

    Skills may be (a) list of dicts with a `name` field (per
    CandidateProfile), (b) list of strings, or (c) absent. All three
    shapes are handled so prompt assembly never raises on profile
    schema drift.
    """
    if not skills or not isinstance(skills, list):
        return "(none listed)"
    rendered: list[str] = []
    for s in skills:
        if isinstance(s, dict):
            name = s.get("name")
            if name:
                depth = s.get("depth")
                rendered.append(f"{name}" + (f" ({depth})" if depth else ""))
        elif isinstance(s, str) and s.strip():
            rendered.append(s.strip())
    return ", ".join(rendered) if rendered else "(none listed)"


def _format_work_history(work_history: Any) -> str:
    """Render up to 6 most recent roles as bullet lines.

    We cap at 6 to keep prompts focused on the candidate's recent
    trajectory; older roles are rarely informative for scoring and only
    inflate token cost. The cap is intentionally a hard slice (not a
    confidence threshold) so prompt size stays bounded.
    """
    if not work_history or not isinstance(work_history, list):
        return "(no work history available)"
    lines: list[str] = []
    for w in work_history[:6]:
        if not isinstance(w, dict):
            continue
        role = _safe_str(w.get("role") or w.get("title"))
        company = _safe_str(w.get("company"))
        start = _safe_str(w.get("start") or w.get("start_date"))
        end = _safe_str(w.get("end") or w.get("end_date") or "present")
        lines.append(f"- {role} @ {company} ({start} → {end})")
    return "\n".join(lines) if lines else "(no work history available)"


def _format_github_signals(github_signals: Any) -> str:
    """Render GitHub signals as a key: value list, or a fallback string.

    Only well-known signal keys are surfaced; unknown keys are passed
    through verbatim so the prompt evolves automatically when Agent 2
    starts emitting new signals (e.g. `recent_activity_score`).
    """
    if not github_signals or not isinstance(github_signals, dict):
        return "(no GitHub signals available)"
    lines: list[str] = []
    for k, v in github_signals.items():
        if v is None or v == "" or v == []:
            continue
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else "(no GitHub signals available)"


def _build_candidate_summary(profile: dict[str, Any]) -> str:
    """One-paragraph candidate summary, used both inline in the prompt
    and returned separately for log/trace correlation."""
    name = _safe_str(profile.get("full_name") or profile.get("name"))
    seniority = _safe_str(profile.get("seniority"))
    years = _safe_str(profile.get("years_experience"))
    location = _safe_str(profile.get("location"))
    github = _safe_str(profile.get("github_username"))
    stage_fit = profile.get("stage_fit") or []
    if isinstance(stage_fit, list) and stage_fit:
        stage_str = ", ".join(str(s) for s in stage_fit)
    else:
        stage_str = "unknown"
    summary = (
        f"{name} — {seniority} engineer, {years} years experience, based in "
        f"{location}. GitHub: {github}. Stage fit: {stage_str}."
    )
    return _truncate(summary, _SUMMARY_CHAR_CAP)


def _render_rubric_dimensions(rubric: dict[str, Any]) -> str:
    """Render each rubric dimension as a numbered block.

    Order is taken verbatim from the rubric — preserving order is what
    lets us correlate dimension positions across replay/audit. We *do
    not* sort dimensions here; the rubric is the source of truth.
    """
    dims = rubric.get("dimensions") if isinstance(rubric, dict) else None
    if not dims or not isinstance(dims, list):
        return "(rubric has no dimensions — workflow precondition violated)"
    blocks: list[str] = []
    for idx, dim in enumerate(dims, start=1):
        if not isinstance(dim, dict):
            continue
        name = _safe_str(dim.get("name"))
        weight_raw = dim.get("weight")
        try:
            weight = float(weight_raw) if weight_raw is not None else 0.0
        except (TypeError, ValueError):
            weight = 0.0
        description = _truncate(
            _safe_str(dim.get("description") or dim.get("desc")),
            _RATIONALE_CHAR_CAP,
        )
        hints = dim.get("evidence_hints") or dim.get("hints") or []
        hints_str = ""
        if isinstance(hints, list) and hints:
            hints_str = "\n   Evidence hints: " + "; ".join(str(h) for h in hints)
        blocks.append(
            f"{idx}. {name} (weight={weight:.2f})\n"
            f"   Description: {description}"
            f"{hints_str}"
        )
    return "\n\n".join(blocks)


_OUTPUT_INSTRUCTIONS = """\
Output a SINGLE JSON object matching this schema exactly. Do NOT wrap it
in markdown fences. Do NOT include any prose before or after the JSON.

{
  "dimensions": [
    {
      "name": "<rubric dimension name — must match input rubric exactly>",
      "score": <integer 0-100>,
      "confidence": <float 0.0-1.0 — your self-assessment of evidence quality>,
      "weight": <float 0.0-1.0 — copy verbatim from the rubric>,
      "rationale": "<one to two sentences explaining the score>",
      "citation": {
        "text": "<verbatim or near-verbatim quote from candidate profile/resume that justifies the score>"
      },
      "evidence_limited": <bool — true ONLY if no GitHub/resume signal exists for this dimension>
    }
    /* one entry per rubric dimension, in rubric order */
  ],
  "strengths": ["<bullet>", "..."],
  "red_flags": ["<bullet>", "..."]
}

IMPORTANT
- DO NOT emit an `overall_match_score` field; the workflow computes it
  deterministically from per-dimension scores and rubric weights.
- Every dimension's `citation.text` MUST be a quote you can point to in
  the candidate profile (resume text, GitHub signals, work history).
  If no quote exists, set `evidence_limited: true` and provide your best
  paraphrase of the absent-evidence in `citation.text` (the citation
  resolver will tier this as a placeholder).
- `confidence` reflects evidence quality, NOT how good the candidate
  looks. A 90/100 score with weak evidence is still low confidence.
- Calibrate confidence aggressively. Confidence < 0.65 triggers a
  self-correction loop that fetches additional evidence; do not inflate.
"""


@ActivityRegistry.register("scorecard", "build_scoring_prompt")
@activity.defn(name="scorecard.build_scoring_prompt")
async def build_scoring_prompt(payload: dict) -> dict:
    """Assemble the initial scoring prompt.

    Args:
        payload: dict matching `BuildScoringPromptInput`.

    Returns:
        dict matching `BuildScoringPromptOutput`.

    Raises:
        pydantic.ValidationError: if `payload` does not match
            `BuildScoringPromptInput`. We surface validation failures
            loudly rather than silently building a degenerate prompt —
            the worst outcome here would be an LLM that scores against
            an empty rubric and returns garbage.
    """
    model = BuildScoringPromptInput.model_validate(payload)

    profile = model.candidate_profile_json or {}
    rubric = model.rubric_json or {}

    candidate_summary = _build_candidate_summary(profile)
    skills_str = _format_skills(profile.get("skills"))
    work_history_str = _format_work_history(profile.get("work_history"))
    github_signals_str = _format_github_signals(profile.get("github_signals"))
    resume_text = _truncate(profile.get("resume_text"), _RESUME_CHAR_CAP)
    jd_text = _truncate(model.job_description, _JD_CHAR_CAP)
    intake_str = _truncate(model.intake_notes, _INTAKE_NOTES_CHAR_CAP) or "(none)"
    rubric_block = _render_rubric_dimensions(rubric)

    prompt = (
        "You are a calibrated technical recruiter scoring a candidate against "
        "a structured rubric for a specific role. Your output drives downstream "
        "ranking and is shown to a human operator; calibrate accordingly.\n\n"
        "=== CANDIDATE SUMMARY ===\n"
        f"{candidate_summary}\n\n"
        "=== CANDIDATE SKILLS ===\n"
        f"{skills_str}\n\n"
        "=== CANDIDATE WORK HISTORY ===\n"
        f"{work_history_str}\n\n"
        "=== GITHUB SIGNALS ===\n"
        f"{github_signals_str}\n\n"
        "=== CANDIDATE RESUME TEXT ===\n"
        f"{resume_text or '(no resume text available)'}\n\n"
        "=== JOB DESCRIPTION ===\n"
        f"{jd_text}\n\n"
        "=== OPERATOR INTAKE NOTES ===\n"
        f"{intake_str}\n\n"
        "=== RUBRIC DIMENSIONS (score every one, in order) ===\n"
        f"{rubric_block}\n\n"
        "=== OUTPUT FORMAT ===\n"
        f"{_OUTPUT_INSTRUCTIONS}"
    )

    LOGGER.info(
        "Built scoring prompt",
        extra={
            "prompt_chars": len(prompt),
            "summary_chars": len(candidate_summary),
            "rubric_dim_count": (
                len(rubric.get("dimensions") or [])
                if isinstance(rubric, dict)
                else 0
            ),
            "resume_truncated": bool(resume_text and _TRUNCATION_MARKER in resume_text),
            "jd_truncated": bool(jd_text and _TRUNCATION_MARKER in jd_text),
        },
    )

    return BuildScoringPromptOutput(
        prompt=prompt,
        candidate_summary=candidate_summary,
    ).model_dump(mode="json")
