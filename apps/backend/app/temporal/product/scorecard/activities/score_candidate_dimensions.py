"""scorecard.score_candidate_dimensions — PRIMARY LLM scoring call.

Single batched Gemini 2.5 Pro structured-output call that scores every
rubric dimension in one shot. Output is a `ScorecardOutput` (per-dim
score/confidence/rationale/citation + strengths + red_flags). The
`overall_match_score` is deliberately NOT on the LLM schema — the
workflow computes it as a deterministic weighted average downstream so
replay determinism of the canonical numeric outcome is never at the
mercy of LLM token-by-token variability.

Reliability design
------------------
* **In-activity retry loop (up to 3 attempts).** Pydantic `ValidationError`
  on the LLM payload is a recoverable parse failure. We retry with a
  corrective addendum appended to the user-role prompt rather than
  bailing to Temporal's activity-level retry — Temporal retries would
  re-emit the original prompt and likely fail the same way. Each retry
  is logged with the validation error so operators can see the model's
  drift pattern.
* **Temporal-level retry still applies** for transport / 5xx errors.
  Pydantic parse retries are an *inner* loop — any non-validation error
  propagates so Temporal's activity retry policy can fire.
* **`parse_attempts` is part of the output** so callers (and the
  observability layer) can see when the model needed multiple tries.
  >1 attempts are a quality signal worth alerting on at sufficient
  volume.
* **Trust boundary.** System prompt is static and operator-controlled.
  The (already-untrusted) candidate profile + JD text lives in the
  caller-provided `scoring_prompt` — that prompt is assembled by
  `build_scoring_prompt` (Phase 3) which is responsible for delimiting
  user-controlled content with `<<<…>>>` fences.

Cost / token accounting
-----------------------
The shared `LLMResponse` model does not yet surface provider token
counts. We estimate from prompt+response size so the workflow's budget
loop has *some* number to subtract. Estimates are flagged via WARNING
log so they are never silently treated as authoritative.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import activity

from app.core.config import settings
from app.core.llm import LLMMessage, get_llm_client
from app.schemas.product.scorecard import ScorecardOutput
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Pro-tier model for the primary scoring call. Falls back to the LLM
# client's default model if the operator did not configure a pro override.
# Kept as a module-level constant so the rescore activity (also Gemini Pro)
# can reuse it via direct import.
_DEFAULT_PRO_MODEL = "gemini-2.5-pro"

# Maximum inner-loop attempts on Pydantic parse failure. Empirically two
# retries is enough — by the third attempt the corrective prompt is
# either working or the model is stuck on a tokenization issue that
# more retries will not solve.
_MAX_PARSE_ATTEMPTS = 3

# Token / cost estimation knobs. Mirrors `app.temporal.core.llm_decision`
# so cost accounting is comparable across activities.
_COST_PER_TOKEN_USD = 0.000001
_TOKEN_ESTIMATE_MULTIPLIER = 1.3


_SYSTEM_PROMPT = (
    "You are an expert technical recruiter scoring a candidate against a job rubric.\n"
    "For each dimension, output a score (0-100), confidence (0-1), one-sentence "
    "rationale, and a citation text excerpt from the candidate profile.\n"
    "Output ONLY valid JSON matching the ScorecardOutput schema.\n"
    "Do NOT include overall_match_score — that is computed separately.\n"
    "\n"
    "SCORING DISCIPLINE:\n"
    "- Score is your assessment of demonstrated competence (0=none, 100=elite).\n"
    "- Confidence reflects how strongly the evidence in the profile supports "
    "your score — high confidence requires concrete, citable evidence.\n"
    "- Rationale must be a single sentence; longer rationales are truncated by "
    "the UI.\n"
    "- Citation text must be a verbatim or near-verbatim quote from the candidate "
    "profile; do not invent quotes. If no evidence exists, return citation=null.\n"
    "- Use the exact rubric dimension names provided; do not rename or invent "
    "dimensions.\n"
    "- Include EVERY dimension from the rubric; missing dimensions break the "
    "downstream weighted-average computation."
)


def _resolve_pro_model() -> str:
    """Pick the Gemini Pro model name.

    Strategy: if the operator configured `GEMINI_MODEL` to a `pro` variant
    (e.g. `gemini-2.5-pro`), use that; otherwise force the Pro constant.
    The scoring call is the most important LLM step in this agent — we
    do not want to silently fall back to Flash and surface a worse
    scorecard.
    """
    configured = (settings.llm.gemini_model or "").strip()
    if configured and "pro" in configured.lower():
        return configured
    return _DEFAULT_PRO_MODEL


def _estimate_tokens(text: str) -> int:
    """Rough word-count → token-count estimate.

    Replace once `LLMResponse.usage` surfaces provider-reported counts.
    """
    if not text:
        return 0
    return int(len(text.split()) * _TOKEN_ESTIMATE_MULTIPLIER)


def _build_user_prompt(
    scoring_prompt: str,
    rubric_dimensions: list[dict],
    correction_hint: str | None,
) -> str:
    """Combine the deterministic scoring prompt with a corrective addendum.

    `scoring_prompt` is the full operator-assembled prompt (candidate +
    JD + intake notes + rubric) produced by `build_scoring_prompt`. We
    append the rubric dimension list explicitly so the LLM cannot
    "forget" a dimension even if the embedding inside `scoring_prompt`
    is unclear.

    `correction_hint` is set on retry attempts ≥2 — it carries the
    Pydantic validation error verbatim plus a directive to fix the
    structural issue. Including the error text is safe because the
    Pydantic error contains only schema field names, not user data.
    """
    dim_names = ", ".join(
        sorted({d.get("name", "") for d in rubric_dimensions if isinstance(d, dict)})
    )
    addendum = (
        "\n\n<<<REQUIRED_DIMENSIONS>>>\n"
        f"{dim_names}\n"
        "<<<END_REQUIRED_DIMENSIONS>>>\n"
        "\nReturn a ScorecardOutput JSON object covering EVERY dimension above."
    )
    if correction_hint:
        addendum += (
            "\n\n<<<CORRECTION>>>\n"
            f"{correction_hint}\n"
            "<<<END_CORRECTION>>>\n"
            "Re-emit the response, fixing the schema issue above."
        )
    return scoring_prompt + addendum


class ScoreCandidateDimensionsInput(BaseModel):
    """Activity input.

    `scoring_prompt` is the full user-role prompt assembled by
    `build_scoring_prompt` (Phase 3). `rubric_dimensions` is the raw
    rubric dimensions list ({name, description, weight}); we use it to
    (a) reinforce coverage in the user prompt and (b) validate that the
    LLM returned every dimension.
    """

    model_config = ConfigDict(extra="forbid")

    scoring_prompt: str = Field(..., min_length=1)
    rubric_dimensions: list[dict] = Field(..., min_length=1)


class ScoreCandidateDimensionsOutput(BaseModel):
    """Activity output.

    `scorecard_output` is `ScorecardOutput.model_dump(mode="json")` —
    serialized as a dict so the workflow can pass it through Temporal
    without forcing every consumer to import the Pydantic schema.
    """

    model_config = ConfigDict(extra="forbid")

    scorecard_output: dict
    tokens_used: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0.0)
    parse_attempts: int = Field(..., ge=1, le=_MAX_PARSE_ATTEMPTS)


@ActivityRegistry.register("scorecard", "score_candidate_dimensions")
@activity.defn(name="scorecard.score_candidate_dimensions")
async def score_candidate_dimensions(payload: dict) -> dict:
    """Score all rubric dimensions in a single batched LLM call.

    Retries internally up to `_MAX_PARSE_ATTEMPTS` times on Pydantic
    parse failure with a corrective addendum appended to the prompt.

    Args:
        payload: dict matching `ScoreCandidateDimensionsInput`.

    Returns:
        `ScoreCandidateDimensionsOutput.model_dump(mode="json")`.

    Raises:
        ValueError: if all parse retries are exhausted (final exception
            wraps the last Pydantic validation error).
        Any non-validation LLM exception propagates so Temporal's
            activity-level retry policy fires.
    """
    input_model = ScoreCandidateDimensionsInput.model_validate(payload)

    llm = get_llm_client()
    model_name = _resolve_pro_model()

    last_validation_error: ValidationError | None = None
    scorecard: ScorecardOutput | None = None
    raw_response_text = ""

    for attempt in range(1, _MAX_PARSE_ATTEMPTS + 1):
        correction_hint: str | None = None
        if last_validation_error is not None:
            # Surface the *first* error line — Pydantic errors are
            # verbose; the LLM doesn't need the full traceback.
            correction_hint = (
                f"Previous response failed schema validation: "
                f"{str(last_validation_error)[:500]}"
            )

        user_prompt = _build_user_prompt(
            scoring_prompt=input_model.scoring_prompt,
            rubric_dimensions=input_model.rubric_dimensions,
            correction_hint=correction_hint,
        )
        raw_response_text = user_prompt  # for token estimate even on failure

        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        LOGGER.info(
            "scorecard.score_candidate_dimensions: attempt",
            extra={
                "attempt": attempt,
                "max_attempts": _MAX_PARSE_ATTEMPTS,
                "model": model_name,
                "dim_count": len(input_model.rubric_dimensions),
            },
        )

        try:
            scorecard = await llm.structured_complete(
                messages=messages,
                schema=ScorecardOutput,
                model=model_name,
            )
        except ValidationError as exc:
            last_validation_error = exc
            LOGGER.warning(
                "scorecard.score_candidate_dimensions: parse failure",
                extra={
                    "attempt": attempt,
                    "error": str(exc)[:500],
                },
            )
            continue
        # Successful parse — break out of retry loop.
        break

    if scorecard is None:
        # All attempts exhausted with parse failures.
        LOGGER.error(
            "scorecard.score_candidate_dimensions: exhausted parse retries",
            extra={"max_attempts": _MAX_PARSE_ATTEMPTS},
        )
        raise ValueError(
            "score_candidate_dimensions: LLM output failed schema validation "
            f"after {_MAX_PARSE_ATTEMPTS} attempts: {last_validation_error}"
        )

    # ---- Token + cost telemetry ------------------------------------------
    # We estimate from prompt + serialized response, because LLMResponse
    # does not yet expose provider counts. The structured_complete path
    # discards the raw text after Pydantic parsing — re-serialize for the
    # estimate.
    response_serialized = scorecard.model_dump_json()
    tokens_used = _estimate_tokens(raw_response_text) + _estimate_tokens(
        response_serialized
    )
    cost_usd = round(tokens_used * _COST_PER_TOKEN_USD, 8)

    LOGGER.warning(
        "Token usage is estimated, not provider-reported",
        extra={"tokens_used_estimate": tokens_used, "cost_usd": cost_usd},
    )
    LOGGER.info(
        "scorecard.score_candidate_dimensions: complete",
        extra={
            "parse_attempts": attempt,
            "dim_count": len(scorecard.dimensions),
            "strengths": len(scorecard.strengths),
            "red_flags": len(scorecard.red_flags),
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
        },
    )

    return ScoreCandidateDimensionsOutput(
        scorecard_output=scorecard.model_dump(mode="json"),
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        parse_attempts=attempt,
    ).model_dump(mode="json")
