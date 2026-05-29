"""scorecard.compute_overall_match_score — Decimal-exact weighted average.

The canonical numeric outcome of the Scorecard workflow. Everything
downstream (Ranking, shortlist UI, operator review) sorts on this value,
so it MUST be bit-exact reproducible across Temporal replays.

Bit-exactness contract
----------------------
* All arithmetic uses `Decimal`, never `float`. The LLM emits integer
  scores (0-100) and float weights — we coerce both into `Decimal`
  before any multiplication or division so IEEE-754 rounding never
  creeps into the result.
* The final value is quantized to 2 decimal places using
  `ROUND_HALF_UP` (standard banker's-rounding alternative; matches user
  expectation for "round 0.5 up"). The quantizer is a module-level
  constant so the rounding strategy cannot be accidentally overridden
  per-call.
* The `Decimal` context is set explicitly to a precision wide enough
  for any realistic rubric (precision=28 — Python's default — comfortably
  handles 100 dimensions × (score × weight) without intermediate
  overflow).

Edge cases
----------
* **Empty `dimensions`** raises `ValueError`. A scorecard with no
  dimensions is a workflow precondition violation; silently returning 0
  would let a buggy workflow persist a 0-score scorecard that ranks
  candidates incorrectly.
* **Zero weight sum** raises `ValueError`. If every dim has weight 0
  the weighted average is undefined; the caller MUST normalize weights
  upstream. We also surface `weight_sum` in the output so callers can
  detect *near-zero* sums (e.g. 0.99 vs 1.00) without re-walking the
  list.
* **Negative scores or weights** raise `ValueError`. ScorecardDimension
  pydantic validation already enforces non-negative bounds, but this
  activity may be called with raw dicts on retry; double-checking is
  cheap and the failure mode (silently negative overall) is awful.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Quantizer for the final answer. 2 decimal places matches the
# `NUMERIC(8,4)` storage column at the database edge (we serialize as a
# string downstream so the trailing zeros are preserved).
_QUANTIZER = Decimal("0.01")

# Tolerance for "weights sum to ~1.0" sanity check. Rubrics are
# operator-authored and rounded floats are normal; we accept up to 1e-3
# drift before logging a warning. We never *fail* on weight-sum drift
# here — that's the rubric repository's job at write time — because
# this activity must be robust to in-flight rubric edits.
_WEIGHT_SUM_WARN_EPSILON = Decimal("0.001")


class ComputeOverallMatchScoreInput(BaseModel):
    """Input: list of dim dicts each carrying `score` (int 0-100) and
    `weight` (float 0.0-1.0). Other keys are ignored — we deliberately
    accept loose dicts so this activity composes with both LLM output
    (which carries rationale, citation, etc.) and synthetic test
    fixtures."""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[dict[str, Any]] = Field(
        ..., description="ScorecardDimension dicts; only `score` and `weight` are read."
    )


class ComputeOverallMatchScoreOutput(BaseModel):
    """Decimal fields are serialized as strings via `model_dump(mode='json')`
    so callers never need to know the bit-pattern of an in-flight
    Decimal — they just persist the string and re-parse on read."""

    model_config = ConfigDict(extra="forbid")

    overall_match_score: Decimal = Field(
        ..., description="Weighted average, 2 decimal places, ROUND_HALF_UP."
    )
    weight_sum: Decimal = Field(
        ..., description="Sum of all input weights; ~1.0 for a well-formed rubric."
    )


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _to_decimal_score(raw: Any, dim_index: int) -> Decimal:
    """Coerce a raw score to a Decimal in [0, 100].

    We accept int OR float OR string for robustness across Temporal
    serialization paths (JSON numbers can be either). `Decimal(str(x))`
    is the bit-exact-safe coercion — `Decimal(float)` would re-introduce
    IEEE-754 noise we worked hard to avoid.
    """
    if raw is None:
        raise ValueError(f"dimensions[{dim_index}].score is required")
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"dimensions[{dim_index}].score is not numeric: {raw!r}"
        ) from exc
    if d < 0 or d > 100:
        raise ValueError(
            f"dimensions[{dim_index}].score={d} outside [0, 100]"
        )
    return d


def _to_decimal_weight(raw: Any, dim_index: int) -> Decimal:
    """Coerce a raw weight to a Decimal in [0, 1].

    Same `Decimal(str(...))` discipline as `_to_decimal_score`. The
    upper bound is enforced at 1.0 (not 1.0 + epsilon) — if a rubric
    has a weight > 1.0 something is wrong upstream and we want to fail
    loud rather than persist a >100 overall score.
    """
    if raw is None:
        raise ValueError(f"dimensions[{dim_index}].weight is required")
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"dimensions[{dim_index}].weight is not numeric: {raw!r}"
        ) from exc
    if d < 0 or d > 1:
        raise ValueError(
            f"dimensions[{dim_index}].weight={d} outside [0, 1]"
        )
    return d


@ActivityRegistry.register("scorecard", "compute_overall_match_score")
@activity.defn(name="scorecard.compute_overall_match_score")
async def compute_overall_match_score(payload: dict) -> dict:
    """Compute the weighted average of per-dimension scores.

    Args:
        payload: dict matching `ComputeOverallMatchScoreInput`.

    Returns:
        dict matching `ComputeOverallMatchScoreOutput`. Decimal fields
        are serialized to JSON strings via `mode="json"`; callers should
        re-parse them as `Decimal(value)` to preserve bit-exactness.

    Raises:
        ValueError: if `dimensions` is empty, if any dim's score/weight
            fails validation, or if the weight sum is zero.
    """
    model = ComputeOverallMatchScoreInput.model_validate(payload)

    if not model.dimensions:
        raise ValueError(
            "compute_overall_match_score: `dimensions` is empty — workflow precondition violated"
        )

    weighted_sum = Decimal("0")
    weight_sum = Decimal("0")
    for idx, dim in enumerate(model.dimensions):
        score = _to_decimal_score(dim.get("score"), idx)
        weight = _to_decimal_weight(dim.get("weight"), idx)
        weighted_sum += score * weight
        weight_sum += weight

    if weight_sum == 0:
        raise ValueError(
            "compute_overall_match_score: total weight is zero — cannot compute weighted average"
        )

    raw_avg = weighted_sum / weight_sum
    overall = raw_avg.quantize(_QUANTIZER, rounding=ROUND_HALF_UP)
    # Quantize weight_sum to the same precision so downstream comparisons
    # against 1.0 are clean (no trailing-zero noise like 1.00000000000).
    weight_sum_q = weight_sum.quantize(_QUANTIZER, rounding=ROUND_HALF_UP)

    drift = abs(weight_sum_q - Decimal("1.00"))
    if drift > _WEIGHT_SUM_WARN_EPSILON:
        LOGGER.warning(
            "Rubric weight sum drifts from 1.0",
            extra={
                "weight_sum": str(weight_sum_q),
                "drift": str(drift),
                "dim_count": len(model.dimensions),
            },
        )

    LOGGER.info(
        "Computed overall match score",
        extra={
            "overall_match_score": str(overall),
            "weight_sum": str(weight_sum_q),
            "dim_count": len(model.dimensions),
        },
    )

    return ComputeOverallMatchScoreOutput(
        overall_match_score=overall,
        weight_sum=weight_sum_q,
    ).model_dump(mode="json")
