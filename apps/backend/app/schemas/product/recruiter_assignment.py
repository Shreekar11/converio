"""Pydantic models for the Recruiter Assignment Agent (Agent 0) workflow IO.

Workflow-internal schemas — decoupled from generated API schemas so the
workflow contract can evolve independently of the HTTP surface.

Replay-determinism: all list fields that carry LLM output use sorted()
in validators where ordering would be non-deterministic. UUID fields are
serialized as strings (mode="json") at the Temporal boundary.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecruiterAssignmentParams(BaseModel):
    """Tunable parameters for the Recruiter Assignment Agent.

    `extra="ignore"` (rather than `forbid`) so the future Planner agent
    can decorate this payload with additional hints (e.g. policy flags,
    cached-assignment pointers) without forcing a schema bump on every
    Planner iteration. Unknown keys are silently dropped at the workflow
    boundary — explicit fields below are still validated strictly.
    """

    model_config = ConfigDict(extra="ignore")

    top_n: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of recruiters to propose to the operator.",
    )
    widen_domain_hops: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Max graph hops for domain-expertise widening when initial pool is thin.",
    )
    reuse_assignment_from: str | None = Field(
        default=None,
        description=(
            "Future Planner field — workflow_id of a prior assignment to reuse. "
            "Accepted for forward compatibility, ignored in MVP."
        ),
    )


class RecruiterAssignmentInput(BaseModel):
    """Payload the Recruiter Assignment workflow receives on `start_workflow`.

    `classification` and `rubric` are passed as plain dicts (not the
    Pydantic models from `app.schemas.product.job`) to keep workflow
    contracts decoupled — the Job Intake workflow serializes its outputs
    via `model_dump(mode="json")` before signaling/starting this workflow.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="UUID of the Job row, as string.")
    classification: dict = Field(
        ...,
        description="RoleClassification.model_dump(mode='json') from Job Intake.",
    )
    rubric: dict = Field(
        ...,
        description="EvaluationRubric.model_dump(mode='json') from Job Intake.",
    )
    params: RecruiterAssignmentParams = Field(default_factory=RecruiterAssignmentParams)
    rejection_notes: str | None = Field(
        default=None,
        description="Populated on re-loop after operator rejection; feeds back into agent prompt.",
    )


class SubScores(BaseModel):
    """Per-dimension fit scores; each component is a 0-100 integer.

    Kept separate from `RecruiterFitScore` so the LLM structured-output
    schema for the scoring activity can target this object directly.
    """

    model_config = ConfigDict(extra="forbid")

    domain: int = Field(..., ge=0, le=100)
    stage: int = Field(..., ge=0, le=100)
    seniority: int = Field(..., ge=0, le=100)
    fill_rate: int = Field(..., ge=0, le=100)
    close_time: int = Field(..., ge=0, le=100)


class RecruiterFitScore(BaseModel):
    """Composite fit score for a single recruiter against the job's rubric."""

    model_config = ConfigDict(extra="forbid")

    recruiter_id: str = Field(..., description="UUID of the recruiter, as string.")
    score: int = Field(..., ge=0, le=100, description="Composite 0-100 fit score.")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in the score; drives proposal quality_flag.",
    )
    rationale: str = Field(
        ...,
        max_length=500,
        description="Short LLM-authored rationale shown in the operator UI.",
    )
    sub_scores: SubScores


class CapacityRecord(BaseModel):
    """Current load snapshot for a recruiter, returned by capacity tool."""

    model_config = ConfigDict(extra="forbid")

    recruiter_id: str
    current_open_roles: int = Field(..., ge=0)
    capacity_max: int = Field(..., ge=1)
    at_capacity: bool


class PlacementRecord(BaseModel):
    """Single historical placement record used in performance evaluation."""

    model_config = ConfigDict(extra="forbid")

    role_title: str
    company_stage: str | None = Field(
        default=None,
        description="CompanyStage value (string-typed to avoid coupling) or None.",
    )
    placed_at: str | None = Field(
        default=None,
        description="ISO 8601 datetime string or None if placement not yet closed.",
    )
    days_to_close: int | None = Field(default=None, ge=0)


class RecruiterCandidate(BaseModel):
    """Summary of one recruiter as returned by search tools.

    Used both in the per-step audit history and in the final operator
    proposal to give reviewers full context on each proposed recruiter
    without a follow-up DB lookup.
    """

    model_config = ConfigDict(extra="forbid")

    recruiter_id: str
    full_name: str
    email: str
    domain_expertise: list[str]
    fill_rate_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    avg_days_to_close: float | None = Field(default=None, ge=0.0)
    total_placements: int = Field(default=0, ge=0)
    status: str = Field(
        ...,
        description="Recruiter status enum value as string (e.g. 'active', 'pending').",
    )
    at_capacity: bool = False


class ProposalEntry(BaseModel):
    """One tool-call step in the LLM audit trail.

    Stored inside `OperatorProposal.audit_trail` so operators can see
    exactly which tools the agent invoked, with truncated arg/result
    summaries for debuggability. Full payloads live in Langfuse traces.
    """

    model_config = ConfigDict(extra="forbid")

    step: int = Field(..., ge=0)
    tool_name: str
    args_summary: str = Field(
        ...,
        max_length=300,
        description="Truncated repr of tool arguments for UI display.",
    )
    result_summary: str = Field(..., max_length=300)
    cost_usd: float = Field(..., ge=0.0)
    tokens_used: int = Field(..., ge=0)


class OperatorProposal(BaseModel):
    """Full proposal payload persisted to `operator_proposals` and served to the UI.

    Self-contained: holds candidates, scores, narratives, and the audit
    trail so the operator UI can render the entire decision context from
    a single row without joins back to ephemeral activity outputs.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    proposed_recruiter_ids: list[str] = Field(
        ...,
        description="Recruiter UUIDs in proposed rank order (best-first).",
    )
    candidates: list[RecruiterCandidate] = Field(
        ...,
        description="Full profiles for the proposed set; same length as proposed_recruiter_ids.",
    )
    fit_scores: list[RecruiterFitScore] = Field(
        ...,
        description="Fit scores for the proposed set.",
    )
    narratives: dict[str, str] = Field(
        ...,
        description="recruiter_id -> LLM narrative summary for operator UI.",
    )
    audit_trail: list[ProposalEntry] = Field(
        ...,
        description="Ordered LLM tool-call history for transparency.",
    )
    quality_flag: str = Field(
        default="high",
        description="Aggregate confidence bucket: 'high' | 'medium' | 'low'.",
    )
    total_tool_calls: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0)
    total_cost_usd: float = Field(..., ge=0.0)


class OperatorApprovalRequest(BaseModel):
    """API input — operator's decision on a proposal.

    Validation rules:
      - `decision` must be 'approve' or 'reject'.
      - On 'approve', at least one of `confirmed_recruiter_ids` or
        `override_set` must be non-empty (operator must commit to a set).
      - On 'reject', no recruiter IDs are required; `notes` is the
        feedback channel back into the agent's re-loop prompt.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., description="'approve' or 'reject'.")
    confirmed_recruiter_ids: list[str] = Field(
        default_factory=list,
        description="Operator-confirmed subset of the proposed set; empty on reject.",
    )
    override_set: list[str] = Field(
        default_factory=list,
        description="Optional operator override — recruiters outside the proposed set.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_decision(self) -> "OperatorApprovalRequest":
        if self.decision not in ("approve", "reject"):
            raise ValueError(
                f"decision must be 'approve' or 'reject', got {self.decision!r}"
            )
        if (
            self.decision == "approve"
            and not self.confirmed_recruiter_ids
            and not self.override_set
        ):
            raise ValueError(
                "approve decision requires confirmed_recruiter_ids or override_set"
            )
        return self


class OperatorProposalResponse(BaseModel):
    """API output for GET /operator/proposals/{id}."""

    job_id: str
    proposal_id: str
    status: str = Field(
        ...,
        description="'awaiting_operator' | 'assigned' | 'rejected_by_operator'.",
    )
    proposal: OperatorProposal
    created_at: str = Field(..., description="ISO 8601 datetime string.")


class RecruiterAssignmentResult(BaseModel):
    """Output of `RecruiterAssignmentWorkflow.run`.

    `status` is typed as `str` (not `RecruiterAssignmentStatus`) because
    the enum lands in W1.2; using a plain string here lets W1.1 schemas
    be merged independently. The workflow itself populates this with a
    `RecruiterAssignmentStatus` value once the enum exists.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str = Field(
        ...,
        description="RecruiterAssignmentStatus value — typed as str until W1.2 lands the enum.",
    )
    assigned_recruiter_ids: list[str]
    assignment_count: int = Field(..., ge=0)
    proposal: OperatorProposal
    operator_decision: dict = Field(
        ...,
        description="Raw OperatorApprovalRequest.model_dump() of the final operator decision.",
    )
    total_loop_iterations: int = Field(..., ge=0)
    total_cost_usd: float = Field(..., ge=0.0)
    total_tokens: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _validate_assignment_count(self) -> "RecruiterAssignmentResult":
        if self.assignment_count != len(self.assigned_recruiter_ids):
            raise ValueError(
                "assignment_count must equal len(assigned_recruiter_ids); "
                f"got {self.assignment_count} vs {len(self.assigned_recruiter_ids)}"
            )
        return self
