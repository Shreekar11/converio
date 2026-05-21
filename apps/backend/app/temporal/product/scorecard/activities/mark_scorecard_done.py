"""scorecard.mark_scorecard_done — terminal control-only activity.

The Scorecard self-correction loop runs as a budgeted LLM-directed
`while` loop. The LLM signals "I am done gathering evidence" by issuing
exactly one tool call to `mark_scorecard_done(reason=...)`. The workflow
inspects the activity's return value (`done=True`) to break the loop and
proceed to citation resolution + persistence.

Design notes
------------
* **Pure pass-through.** No LLM call, no DB write, no GitHub call. The
  activity exists *only* so the LLM tool-calling registry has a uniform
  shape for the terminate signal (every "decision" the LLM makes is a
  tool call; otherwise the planner would need a second decision modality
  just for termination).
* **Replay-deterministic.** Identical input produces identical output
  bytewise — no clock reads, no random IDs, no IO. Safe to retry; safe
  to replay.
* **Reasons are operator-surfaced.** `termination_reason` lands on the
  persisted `ScorecardWorkflowResult` so the operator UI can explain
  low-confidence scorecards ("budget_exhausted" implies the loop ran out
  of budget before re-scoring weak dims; the scorecard should not be
  treated as definitive).
* **Out-of-loop termination.** `budget_exhausted` is only produced by
  the workflow itself when guardrails fire; it is included in the
  `Literal` for symmetry with `ScorecardWorkflowResult.termination_reason`
  so the workflow can call the same activity to record an injected
  termination event in the call history.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class MarkScorecardDoneInput(BaseModel):
    """Activity input.

    `reason` is a closed enum so that downstream consumers (UI, ranking
    agent) can dispatch on it without dealing with free-form strings.
    `message` is an optional human-readable note the LLM may attach to
    explain *why* it chose the reason (e.g. "two consecutive rescore
    calls added <0.05 confidence"). It is logged but not used for any
    branching.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "all_dims_confident",
        "diminishing_returns",
        "budget_imminent",
        "budget_exhausted",
    ]
    message: str | None = Field(
        default=None,
        max_length=500,
        description="Optional LLM-authored note explaining the chosen reason.",
    )


class MarkScorecardDoneOutput(BaseModel):
    """Activity output. `done` is always True — the workflow uses it
    purely as a typed sentinel to break its loop. Returning a structured
    output (rather than `None`) keeps the activity result schema-stable
    across replay and gives Temporal history a non-null payload to record.
    """

    model_config = ConfigDict(extra="forbid")

    done: bool = True
    reason: str


@ActivityRegistry.register("scorecard", "mark_scorecard_done")
@activity.defn(name="scorecard.mark_scorecard_done")
async def mark_scorecard_done(payload: dict) -> dict:
    """Signal loop termination. Returns `done=True` and echoes the reason.

    Args:
        payload: dict matching `MarkScorecardDoneInput`.

    Returns:
        `MarkScorecardDoneOutput.model_dump(mode="json")`.

    Raises:
        pydantic.ValidationError: if the payload is malformed (caller
            passed an unsupported `reason`). Caller is the LLM tool
            dispatcher, which enforces the same enum upstream — a
            validation error here is a real bug, not a transient failure.
    """
    input_model = MarkScorecardDoneInput.model_validate(payload)
    LOGGER.info(
        "scorecard.mark_scorecard_done",
        # `message` is a reserved key on stdlib LogRecord — using it inside
        # `extra` raises KeyError. Rename to `note` for the log path.
        extra={"reason": input_model.reason, "note": input_model.message},
    )
    return MarkScorecardDoneOutput(
        done=True,
        reason=input_model.reason,
    ).model_dump(mode="json")
