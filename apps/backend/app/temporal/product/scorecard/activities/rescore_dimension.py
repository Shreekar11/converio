"""scorecard.rescore_dimension — re-score a single dim given new evidence.

After `select_evidence_source` picks a fetcher and the workflow runs it,
the resulting evidence dict (any of the 5 fetcher outputs) is fed back
to the LLM along with the original score/confidence/rationale. The LLM
returns an updated `ScorecardDimension` (without `weight`, which is
fixed at scorecard time and merged back in by the workflow).

Memoization
-----------
This activity is the most expensive per-call LLM step in the scorecard
loop (Gemini 2.5 Pro). If the workflow replays — Temporal *will* replay
activities under non-deterministic workflow code — re-running the LLM
call would (a) double the cost and (b) potentially shift the score
fractionally and break replay equality.

We hash `(dimension_name, evidence)` with SHA-256 and cache the result
in a module-level dict keyed by `workflow_id + evidence_hash`. On a
replay the same workflow_id + same evidence dict hits the cache and
returns the prior result bytewise.

Caveats (acceptable for PoW):
* Cache is in-process. A worker restart loses it. For replay during a
  single worker lifetime — the common case — the cache works. For
  cross-restart determinism we would need a persistent memo store
  (out of scope for v1).
* Cache key includes `workflow_id` to avoid cross-workflow leakage:
  two scorecards for the same candidate but different rubrics would
  otherwise share cached results, which is wrong.
* The cache is bounded loosely (insertion-ordered dict with a soft cap)
  to prevent unbounded growth in long-lived worker processes.
"""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import activity

from app.core.config import settings
from app.core.llm import LLMMessage, get_llm_client
from app.schemas.product.scorecard import ScorecardDimension
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Reuse the Pro-model resolver semantics from score_candidate_dimensions.
# Importing the helper would create a circular concern if that module
# ever pulls from rescore — keep the constants local for symmetry.
_DEFAULT_PRO_MODEL = "gemini-2.5-pro"

_MAX_PARSE_ATTEMPTS = 3

# Module-level memo cache. Insertion-ordered; bounded by `_MEMO_MAX_SIZE`.
# Concurrent access from multiple activities in the same worker is fine
# because dict assignment in CPython is atomic at the bytecode level and
# we never iterate the dict while mutating it.
_MEMO_CACHE: dict[str, dict] = {}
_MEMO_MAX_SIZE = 1024

_COST_PER_TOKEN_USD = 0.000001
_TOKEN_ESTIMATE_MULTIPLIER = 1.3

_SYSTEM_PROMPT = (
    "You are re-scoring a SINGLE rubric dimension for a candidate, given new "
    "evidence fetched by another tool.\n"
    "\n"
    "Output ONLY valid JSON matching the ScorecardDimension schema fields: "
    "name, score (0-100), confidence (0-1), weight (echo the original weight), "
    "rationale (1 sentence), citation (verbatim quote from evidence or null), "
    "evidence_limited (bool).\n"
    "\n"
    "RULES:\n"
    "- Keep the dimension name EXACTLY as given; do not rename.\n"
    "- The new score may be higher OR lower than the original — base it on "
    "what the evidence supports, not on a desire to 'help' the candidate.\n"
    "- Confidence must reflect the strength of the evidence. If the evidence "
    "is empty or off-topic, keep confidence near the original value and set "
    "evidence_limited=true.\n"
    "- Citation must be a near-verbatim excerpt from the supplied evidence; "
    "do not invent quotes.\n"
    "- Rationale is a single sentence; longer rationales are truncated."
)


def _resolve_pro_model() -> str:
    configured = (settings.llm.gemini_model or "").strip()
    if configured and "pro" in configured.lower():
        return configured
    return _DEFAULT_PRO_MODEL


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text.split()) * _TOKEN_ESTIMATE_MULTIPLIER)


def _compute_evidence_hash(dimension_name: str, evidence: dict) -> str:
    """SHA-256 of the canonical serialization of (dim, evidence).

    `sort_keys=True` makes the JSON dump order-independent so two
    equivalent evidence dicts produce the same hash even if their key
    insertion order differs. `default=str` swallows datetimes / UUIDs
    that occasionally leak through fetcher outputs.
    """
    payload = json.dumps(
        {"dim": dimension_name, "evidence": evidence},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _memo_key(workflow_id: str | None, evidence_hash: str) -> str:
    """Scope memo by workflow_id so different scorecards (e.g. different
    rubrics for the same candidate) do not collide.
    """
    return f"{workflow_id or 'no-wf'}::{evidence_hash}"


def _memo_get(key: str) -> dict | None:
    return _MEMO_CACHE.get(key)


def _memo_put(key: str, value: dict) -> None:
    """Bounded insert. When the cache exceeds `_MEMO_MAX_SIZE` we drop the
    oldest entry (insertion-ordered dict). Crude but adequate: the cache
    is per-process and per-worker, and a worker handling >1024 distinct
    `(workflow_id, evidence_hash)` pairs is already serving many
    scorecards.
    """
    if key in _MEMO_CACHE:
        # Refresh insertion order by re-inserting.
        _MEMO_CACHE.pop(key, None)
    elif len(_MEMO_CACHE) >= _MEMO_MAX_SIZE:
        try:
            oldest = next(iter(_MEMO_CACHE))
            _MEMO_CACHE.pop(oldest, None)
        except StopIteration:  # pragma: no cover — defensive
            pass
    _MEMO_CACHE[key] = value


class RescoreDimensionInput(BaseModel):
    """Activity input."""

    model_config = ConfigDict(extra="forbid")

    dimension_name: str = Field(..., min_length=1)
    original_score: int = Field(..., ge=0, le=100)
    original_confidence: float = Field(..., ge=0.0, le=1.0)
    original_rationale: str
    original_weight: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Rubric weight; echoed back through the LLM-emitted dim.",
    )
    evidence: dict = Field(..., description="Output dict from one of the 5 fetchers.")
    candidate_profile_summary: str = Field(default="", max_length=4000)


class RescoreDimensionOutput(BaseModel):
    """Activity output. `evidence_hash` is exposed so the workflow can
    use it to deduplicate "did we already rescore on this evidence?"
    bookkeeping in addition to the in-process memo cache.
    """

    model_config = ConfigDict(extra="forbid")

    dimension_name: str
    new_score: int = Field(..., ge=0, le=100)
    new_confidence: float = Field(..., ge=0.0, le=1.0)
    new_rationale: str
    new_citation_text: str | None = None
    tokens_used: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0.0)
    evidence_hash: str = Field(..., min_length=64, max_length=64)


def _build_user_prompt(
    *,
    dim_name: str,
    original_score: int,
    original_confidence: float,
    original_rationale: str,
    original_weight: float,
    evidence: dict,
    candidate_profile_summary: str,
    correction_hint: str | None,
) -> str:
    base = (
        "<<<DIMENSION>>>\n"
        f"name: {dim_name}\n"
        f"original_score: {original_score}\n"
        f"original_confidence: {original_confidence}\n"
        f"original_weight: {original_weight}\n"
        f"original_rationale: {original_rationale}\n"
        "<<<END_DIMENSION>>>\n"
        "\n"
        "<<<NEW_EVIDENCE>>>\n"
        f"{json.dumps(evidence, sort_keys=True, default=str)}\n"
        "<<<END_NEW_EVIDENCE>>>\n"
        "\n"
        "<<<CANDIDATE_PROFILE_SUMMARY>>>\n"
        f"{candidate_profile_summary or '(no summary provided)'}\n"
        "<<<END_CANDIDATE_PROFILE_SUMMARY>>>\n"
        "\n"
        "Re-score this dimension given the new evidence."
    )
    if correction_hint:
        base += (
            "\n\n<<<CORRECTION>>>\n"
            f"{correction_hint}\n"
            "<<<END_CORRECTION>>>\n"
            "Re-emit the response, fixing the schema issue above."
        )
    return base


@ActivityRegistry.register("scorecard", "rescore_dimension")
@activity.defn(name="scorecard.rescore_dimension")
async def rescore_dimension(payload: dict) -> dict:
    """Re-score a single dimension. Memoizes on
    `(workflow_id, dimension_name, evidence_hash)` for replay determinism.

    Args:
        payload: dict matching `RescoreDimensionInput`.

    Returns:
        `RescoreDimensionOutput.model_dump(mode="json")`.

    Raises:
        ValueError: if the LLM output fails schema validation across all
            retry attempts.
    """
    input_model = RescoreDimensionInput.model_validate(payload)

    evidence_hash = _compute_evidence_hash(
        input_model.dimension_name, input_model.evidence
    )

    # ---- Memo lookup -----------------------------------------------------
    # `activity.info()` raises outside an activity context. In tests we
    # may call the function directly — degrade gracefully so the cache
    # key just uses `"no-wf"`.
    workflow_id: str | None = None
    try:
        info = activity.info()
        workflow_id = info.workflow_id
    except Exception:  # pragma: no cover — direct call path
        workflow_id = None

    cache_key = _memo_key(workflow_id, evidence_hash)
    cached = _memo_get(cache_key)
    if cached is not None:
        LOGGER.info(
            "scorecard.rescore_dimension: memo hit",
            extra={
                "dim": input_model.dimension_name,
                "workflow_id": workflow_id,
                "evidence_hash": evidence_hash,
            },
        )
        return cached

    # ---- LLM call with in-activity parse retries -------------------------
    llm = get_llm_client()
    model_name = _resolve_pro_model()

    last_error: ValidationError | None = None
    dim_result: ScorecardDimension | None = None
    last_prompt = ""

    for attempt in range(1, _MAX_PARSE_ATTEMPTS + 1):
        correction_hint: str | None = None
        if last_error is not None:
            correction_hint = (
                f"Previous response failed schema validation: "
                f"{str(last_error)[:500]}"
            )

        user_prompt = _build_user_prompt(
            dim_name=input_model.dimension_name,
            original_score=input_model.original_score,
            original_confidence=input_model.original_confidence,
            original_rationale=input_model.original_rationale,
            original_weight=input_model.original_weight,
            evidence=input_model.evidence,
            candidate_profile_summary=input_model.candidate_profile_summary,
            correction_hint=correction_hint,
        )
        last_prompt = user_prompt

        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        LOGGER.info(
            "scorecard.rescore_dimension: attempt",
            extra={
                "dim": input_model.dimension_name,
                "attempt": attempt,
                "max_attempts": _MAX_PARSE_ATTEMPTS,
                "model": model_name,
                "evidence_hash": evidence_hash,
            },
        )

        try:
            dim_result = await llm.structured_complete(
                messages=messages,
                schema=ScorecardDimension,
                model=model_name,
            )
        except ValidationError as exc:
            last_error = exc
            LOGGER.warning(
                "scorecard.rescore_dimension: parse failure",
                extra={
                    "dim": input_model.dimension_name,
                    "attempt": attempt,
                    "error": str(exc)[:500],
                },
            )
            continue
        break

    if dim_result is None:
        LOGGER.error(
            "scorecard.rescore_dimension: exhausted parse retries",
            extra={
                "dim": input_model.dimension_name,
                "max_attempts": _MAX_PARSE_ATTEMPTS,
            },
        )
        raise ValueError(
            f"rescore_dimension: LLM output failed schema validation after "
            f"{_MAX_PARSE_ATTEMPTS} attempts: {last_error}"
        )

    assert dim_result is not None  # narrowing: raise above guarantees non-None

    # Defensive: if the LLM renamed the dim, snap it back. The downstream
    # merge logic keys on dim name so a drifted name would silently drop
    # the rescore.
    if dim_result.name != input_model.dimension_name:
        LOGGER.warning(
            "scorecard.rescore_dimension: LLM renamed dim; correcting",
            extra={
                "expected": input_model.dimension_name,
                "got": dim_result.name,
            },
        )
        dim_result = dim_result.model_copy(update={"name": input_model.dimension_name})

    assert isinstance(dim_result, ScorecardDimension)  # narrowing after model_copy branch

    # ---- Token + cost telemetry ------------------------------------------
    response_serialized = dim_result.model_dump_json()
    tokens_used = _estimate_tokens(last_prompt) + _estimate_tokens(response_serialized)
    cost_usd = round(tokens_used * _COST_PER_TOKEN_USD, 8)

    LOGGER.warning(
        "Token usage is estimated, not provider-reported",
        extra={"tokens_used_estimate": tokens_used, "cost_usd": cost_usd},
    )

    citation_text: str | None = None
    if dim_result.citation is not None and dim_result.citation.text:
        citation_text = dim_result.citation.text

    output = RescoreDimensionOutput(
        dimension_name=input_model.dimension_name,
        new_score=dim_result.score,
        new_confidence=dim_result.confidence,
        new_rationale=dim_result.rationale,
        new_citation_text=citation_text,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        evidence_hash=evidence_hash,
    )
    serialized = output.model_dump(mode="json")

    # ---- Cache + return --------------------------------------------------
    _memo_put(cache_key, serialized)
    LOGGER.info(
        "scorecard.rescore_dimension: complete",
        extra={
            "dim": input_model.dimension_name,
            "new_score": dim_result.score,
            "new_confidence": dim_result.confidence,
            "evidence_hash": evidence_hash,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
            "memoized": True,
        },
    )
    return serialized
