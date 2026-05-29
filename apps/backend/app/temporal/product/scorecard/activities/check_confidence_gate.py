"""scorecard.check_confidence_gate — filter dimensions below confidence threshold.

Pure deterministic filter. No LLM, no DB, no network. The output is a
function of the input alone, which makes it safe to retry indefinitely
and trivially correct on Temporal replay.

Gate semantics
--------------
A dimension is flagged as "low confidence" iff BOTH:

1. `confidence < CONFIDENCE_THRESHOLD` (0.65 by spec, plan §11), AND
2. `evidence_limited is not True`.

The second clause is the critical one — `evidence_limited=True` means
the LLM already declared that no fetcher tool exists for this dimension
in the MVP toolset. Re-scoring it would burn budget on a hopeless retry
because there is no additional evidence to feed the rescore prompt.
Flagging them as low-confidence would also bias the operator UI's
"this scorecard is shaky" indicator unfairly: the issue isn't model
confidence but the v1 tool catalog.

We surface the count separately (`low_conf_count`) so callers can
short-circuit the self-correction loop without re-counting the list.
`all_confident` is `True` iff `low_conf_count == 0` — exposing both is
deliberate redundancy that keeps workflow conditionals readable.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.schemas.product.scorecard import LowConfDim, ScorecardDimension
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Spec'd threshold from `docs/plans/scorecard_agent_plan.md` §11.
# Kept as a module-level constant — changing it requires a deliberate
# code change + PR, not a config tweak — because shifting this number
# changes the agent's escalation rate to the self-correction loop and
# therefore its budget profile. Treat as a hard contract.
CONFIDENCE_THRESHOLD: float = 0.65


class CheckConfidenceGateInput(BaseModel):
    """Input: list of `ScorecardDimension`-shaped dicts.

    Dicts (not Pydantic models) cross the Temporal activity boundary; we
    re-validate via `ScorecardDimension.model_validate` inside the
    activity so schema drift in upstream activities surfaces as a clean
    ValidationError rather than a runtime AttributeError.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[dict[str, Any]] = Field(
        ...,
        description="List of ScorecardDimension dicts (LLM output, already validated upstream).",
    )


class CheckConfidenceGateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low_confidence_dimensions: list[dict[str, Any]] = Field(
        ...,
        description="LowConfDim dicts for dims below the confidence threshold.",
    )
    all_confident: bool = Field(
        ...,
        description="True iff every dim is at or above the confidence threshold.",
    )
    low_conf_count: int = Field(
        ..., ge=0, description="Count of low-confidence dims; redundant with len(list)."
    )


@ActivityRegistry.register("scorecard", "check_confidence_gate")
@activity.defn(name="scorecard.check_confidence_gate")
async def check_confidence_gate(payload: dict) -> dict:
    """Return dims whose confidence is below threshold and not evidence-limited.

    Args:
        payload: dict matching `CheckConfidenceGateInput`.

    Returns:
        dict matching `CheckConfidenceGateOutput`.

    Raises:
        pydantic.ValidationError: on malformed input OR on any dim that
            fails `ScorecardDimension` validation. We deliberately do
            not silently skip malformed dims — losing a dim from the
            confidence gate would falsely mark a scorecard as
            "all confident" and short-circuit self-correction.
    """
    model = CheckConfidenceGateInput.model_validate(payload)

    # Strict per-dim validation. If the upstream scoring activity drifts
    # its schema, the workflow should fail loud and visible rather than
    # silently dropping dims from the gate.
    parsed: list[ScorecardDimension] = [
        ScorecardDimension.model_validate(d) for d in model.dimensions
    ]

    low_conf: list[LowConfDim] = []
    for dim in parsed:
        if dim.evidence_limited:
            # Honor the v1 toolset reality — see module docstring.
            continue
        if dim.confidence < CONFIDENCE_THRESHOLD:
            low_conf.append(
                LowConfDim(
                    name=dim.name,
                    current_confidence=dim.confidence,
                    current_score=dim.score,
                    reason=(
                        f"confidence {dim.confidence:.2f} below threshold "
                        f"{CONFIDENCE_THRESHOLD}"
                    ),
                )
            )

    low_conf_count = len(low_conf)
    all_confident = low_conf_count == 0

    LOGGER.info(
        "Confidence gate evaluated",
        extra={
            "total_dims": len(parsed),
            "low_conf_count": low_conf_count,
            "all_confident": all_confident,
            "threshold": CONFIDENCE_THRESHOLD,
        },
    )

    return CheckConfidenceGateOutput(
        low_confidence_dimensions=[lc.model_dump(mode="json") for lc in low_conf],
        all_confident=all_confident,
        low_conf_count=low_conf_count,
    ).model_dump(mode="json")
