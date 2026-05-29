"""Pydantic models for the Scorecard Generator Agent (Agent 4) workflow IO.

Workflow-internal schemas — decoupled from generated API schemas so the
workflow contract can evolve independently of the HTTP surface.

Replay-determinism: dimension ordering is preserved as the LLM emits it,
but the workflow is expected to sort `dimensions_rescored` and any other
list of strings derived from non-deterministic LLM output before
returning a `ScorecardWorkflowResult`. UUID fields cross the Temporal
boundary as native `UUID`; activities serialize via `model_dump(mode="json")`
at the persistence boundary.

The LLM emits a `ScorecardOutput` (no overall score). The workflow then
computes `overall_match_score` deterministically as a weighted average of
per-dimension scores, so Temporal replay never depends on LLM output for
the canonical numeric outcome.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Citation(BaseModel):
    """Pointer back to the evidence span that justified a dimension's score.

    `resolution_method` records HOW the citation was produced so the
    operator UI can flag low-quality citations:

    * `direct_text_match` — citation text was found verbatim in fetched
      evidence (resume, README, etc.).
    * `pgvector_semantic` — citation was matched by semantic similarity
      via pgvector; offsets are approximate.
    * `placeholder` — LLM produced text without a verifiable source;
      treated as evidence-limited and surfaced to the operator.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., description="Verbatim or near-verbatim evidence quote.")
    char_offset_start: int | None = Field(
        default=None,
        ge=0,
        description="Start offset into the source document; None for semantic matches.",
    )
    char_offset_end: int | None = Field(
        default=None,
        ge=0,
        description="End offset into the source document; None for semantic matches.",
    )
    resolution_method: Literal[
        "direct_text_match", "pgvector_semantic", "placeholder"
    ] = "placeholder"
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Resolution confidence (only meaningful for pgvector_semantic).",
    )


class ScorecardDimension(BaseModel):
    """A single rubric-dimension score with confidence, weight, and citation.

    `evidence_limited=True` signals that no fetcher tool was available for
    this dimension in the v1 MVP toolset (i.e., the LLM scored from resume
    text alone). The operator UI dims the row and the self-correction
    loop deliberately skips re-scoring these dimensions to avoid burning
    budget on hopeless re-tries.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Rubric dimension name (matches RubricDimension.name).")
    score: int = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0.0, le=1.0)
    weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Rubric weight pinned at scorecard time; sums to 1.0 across dims.",
    )
    rationale: str = Field(..., description="LLM-authored justification, shown in UI.")
    citation: Citation | None = None
    evidence_limited: bool = Field(
        default=False,
        description="True when no fetcher tool was available for this dimension in v1.",
    )


class LowConfDim(BaseModel):
    """Identifier for a dimension that triggered self-correction.

    Used as an intermediate signal between the initial scoring activity and
    the self-correction loop. Carries enough context for the LLM prompt to
    explain WHY a dimension is being re-scored without re-fetching state.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    current_confidence: float = Field(..., ge=0.0, le=1.0)
    current_score: int = Field(..., ge=0, le=100)
    reason: str | None = Field(
        default=None,
        description="Optional explanation for why confidence is low (e.g., missing evidence).",
    )


class EvidenceSourceChoice(BaseModel):
    """LLM-planned next evidence-fetch action.

    Emitted by the self-correction planner when it decides which tool to
    invoke to raise a dimension's confidence. `tool_name` must match one
    of the 5 MVP fetcher tool names registered in the activity catalog;
    enforcement happens at the activity-dispatch boundary, not here, so
    this schema stays decoupled from the tool registry.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(
        ...,
        description="One of the 5 MVP fetcher tool names (validated at dispatch).",
    )
    reasoning: str = Field(
        ...,
        description="LLM rationale for choosing this tool; logged to Langfuse.",
    )
    keyword_filter: list[str] | None = Field(
        default=None,
        description="Optional keyword filter for tools that support it (e.g. fetch_repo_readmes).",
    )


class ScorecardOutput(BaseModel):
    """LLM-produced scorecard output.

    NOTE: `overall_match_score` is intentionally NOT on this model — it is
    computed deterministically by the workflow as a weighted average of
    per-dimension scores. Keeping it off the LLM output schema is what
    guarantees replay determinism of the canonical numeric outcome and
    prevents the LLM from drifting the displayed score independently of
    its own per-dimension scores.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[ScorecardDimension] = Field(
        ...,
        description="One entry per rubric dimension; order follows the rubric.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="LLM-authored highlight bullets for the candidate.",
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="LLM-authored concern bullets for the candidate.",
    )


class ScorecardWorkflowInput(BaseModel):
    """Payload the Scorecard workflow receives on `start_workflow`.

    `submission_id` is null for sourcing-agent candidates (no recruiter
    submission). `rubric_id` is required so the scorecard is pinned to a
    specific rubric version; reevaluation under a new rubric must start a
    fresh workflow with a new `rubric_id` and produces a new Scorecard row.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    candidate_id: UUID
    rubric_id: UUID
    submission_id: UUID | None = Field(
        default=None,
        description="Null for sourcing-agent candidates (no recruiter submission).",
    )


class ScorecardWorkflowResult(BaseModel):
    """Output of `ScorecardWorkflow.run`.

    `termination_reason` records why the self-correction loop exited; the
    operator UI surfaces this to explain low-confidence scorecards (e.g.
    `budget_exhausted` means the agent ran out of budget before fully
    re-scoring weak dimensions, and the operator should not treat the
    scorecard as definitive).
    """

    model_config = ConfigDict(extra="forbid")

    scorecard_id: UUID
    overall_match_score: Decimal = Field(
        ...,
        description="Deterministic weighted average of per-dimension scores; 0-100.",
    )
    self_correction_triggered: bool
    dimensions_rescored: list[str] = Field(
        ...,
        description="Names of dimensions that went through self-correction (sorted).",
    )
    tool_call_count: int = Field(..., ge=0)
    total_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    termination_reason: Literal[
        "all_dims_confident",
        "diminishing_returns",
        "budget_exhausted",
        "budget_imminent",
    ]
