"""Deterministic weighted ranking of scored recruiters. No LLM call — pure Python."""
from __future__ import annotations

from temporalio import activity

from app.schemas.product.recruiter_assignment import RecruiterFitScore
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Weighted ranking coefficients. The composite weighted_score is a convex
# combination of the 0-100 sub_scores; weights MUST sum to 1.0 so the result
# remains in [0, 100] and is directly comparable to the LLM-authored
# `score` field on RecruiterFitScore.
_WEIGHT_DOMAIN: float = 0.35
_WEIGHT_FILL_RATE: float = 0.25
_WEIGHT_STAGE: float = 0.20
_WEIGHT_CLOSE_TIME: float = 0.10
_WEIGHT_SENIORITY: float = 0.10

# Module-load invariant: any future tweak to the weights must preserve the
# convex-combination property. Use a small epsilon to absorb FP noise.
assert (
    abs(
        (
            _WEIGHT_DOMAIN
            + _WEIGHT_FILL_RATE
            + _WEIGHT_STAGE
            + _WEIGHT_CLOSE_TIME
            + _WEIGHT_SENIORITY
        )
        - 1.0
    )
    < 1e-9
), "rank_and_select_recruiters weights must sum to 1.0"

_TOP_N_DEFAULT = 5
_TOP_N_MIN = 1
_TOP_N_MAX = 10


def _clamp_top_n(value: int | None) -> int:
    """Clamp `top_n` into the supported [1, 10] window.

    Mirrors the validation surface of `RecruiterAssignmentParams.top_n` so
    callers that bypass the params object (e.g. direct activity invocation
    in tests) still get safe behavior.
    """
    if value is None:
        return _TOP_N_DEFAULT
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return _TOP_N_DEFAULT
    if as_int < _TOP_N_MIN:
        return _TOP_N_MIN
    if as_int > _TOP_N_MAX:
        return _TOP_N_MAX
    return as_int


@ActivityRegistry.register("recruiter_assignment", "rank_and_select_recruiters")
@activity.defn(name="recruiter_assignment.rank_and_select_recruiters")
async def rank_and_select_recruiters(payload: dict) -> dict:
    """Rank scored recruiters by weighted sub-scores and return the top N.

    Pure-Python deterministic ranking — no DB or LLM calls. Safe to retry
    indefinitely; the output is a function of the input alone.

    Args:
        payload: dict with keys
            - scores (list[dict]): list of `RecruiterFitScore.model_dump()`
              entries from the `score_recruiter_fit` activity.
            - top_n (int, optional): number of top recruiters to return;
              clamped to [1, 10]. Defaults to 5.

    Returns:
        dict with keys
            - ranked (list[dict]): top-N entries, descending by
              `weighted_score`. Each entry contains:
                * recruiter_id (str)
                * weighted_score (float, rounded to 2 decimals)
                * original_score (int) — the LLM composite score
                * confidence (float)
                * rationale (str)
            - top_n (int): the effective top_n actually applied.
            - total_scored (int): number of input scores parsed.

    Raises:
        ValidationError: If any entry in `scores` fails RecruiterFitScore
            validation. Surfacing schema drift loudly is preferable to
            silently dropping recruiters from consideration.
    """
    raw_scores = payload.get("scores") or []
    if not isinstance(raw_scores, list):
        raise ValueError("payload.scores must be a list of RecruiterFitScore dicts")

    top_n = _clamp_top_n(payload.get("top_n"))

    # Strict parse — let pydantic raise on schema drift rather than silently
    # excluding malformed entries from the ranking.
    parsed: list[RecruiterFitScore] = [
        RecruiterFitScore.model_validate(entry) for entry in raw_scores
    ]

    LOGGER.info(
        "Ranking recruiter fit scores",
        extra={
            "total_scored": len(parsed),
            "top_n": top_n,
        },
    )

    ranked_entries: list[dict] = []
    for fit in parsed:
        weighted = (
            fit.sub_scores.domain * _WEIGHT_DOMAIN
            + fit.sub_scores.fill_rate * _WEIGHT_FILL_RATE
            + fit.sub_scores.stage * _WEIGHT_STAGE
            + fit.sub_scores.close_time * _WEIGHT_CLOSE_TIME
            + fit.sub_scores.seniority * _WEIGHT_SENIORITY
        )
        ranked_entries.append(
            {
                "recruiter_id": fit.recruiter_id,
                "weighted_score": round(weighted, 2),
                "original_score": fit.score,
                "confidence": fit.confidence,
                "rationale": fit.rationale,
            }
        )

    # Stable sort: Python's `sorted` is stable, so input order serves as the
    # deterministic tiebreaker when weighted_scores are equal — important for
    # Temporal replay determinism on re-runs of the workflow.
    ranked_entries.sort(key=lambda e: e["weighted_score"], reverse=True)

    top_slice = ranked_entries[:top_n]

    LOGGER.info(
        "Recruiter ranking complete",
        extra={
            "total_scored": len(parsed),
            "top_n": top_n,
            "returned": len(top_slice),
            "top_recruiter_id": top_slice[0]["recruiter_id"] if top_slice else None,
            "top_weighted_score": top_slice[0]["weighted_score"] if top_slice else None,
        },
    )

    return {
        "ranked": top_slice,
        "top_n": top_n,
        "total_scored": len(parsed),
    }
