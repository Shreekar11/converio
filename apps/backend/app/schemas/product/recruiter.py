"""Pydantic schemas for Agent 0 prerequisite (Recruiter Indexing Workflow) IO.

All models must be JSON-serializable — no ORM objects, no non-serializable types.
Activity inputs/outputs and workflow IO use these models exclusively.

Mirrors `apps/backend/app/schemas/product/candidate.py` conventions. Reuses
existing enums from `app.schemas.enums` — no new Domain enum (Domain values
are locked to `RoleCategory` per plan decision).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.enums import (
    CompanyStage,
    RecruitedFundingStage,
    RoleCategory,
    WorkspaceType,
)


class RecruiterClientItem(BaseModel):
    """Single past-client entry from recruiter onboarding wizard."""

    client_company_name: str
    description: str | None = None
    role_focus: list[str] = Field(default_factory=list)


class RecruiterPlacementItem(BaseModel):
    """Single past-placement entry from recruiter onboarding wizard.

    Historical claim — `candidate_name` is freeform, not linked to candidates table.
    """

    candidate_name: str
    company_name: str
    company_stage: CompanyStage | None = None
    role_title: str
    placed_at: str | None = None  # ISO date string or freeform
    description: str | None = None


class RecruiterProfile(BaseModel):
    """Structured recruiter profile — full wizard payload.

    Wizard writes Recruiter + RecruiterClient + RecruiterPlacement rows synchronously;
    the indexing workflow consumes this profile to derive enrichment (metrics,
    embedding, graph indexing, credibility scoring) — never inserts the recruiter row.
    """

    recruiter_id: str  # UUID string
    full_name: str
    email: str
    linkedin_url: str | None = None
    bio: str | None = None
    domain_expertise: list[RoleCategory] = Field(default_factory=list)
    workspace_type: WorkspaceType | None = None
    recruited_funding_stage: RecruitedFundingStage | None = None
    past_clients: list[RecruiterClientItem] = Field(default_factory=list)
    past_placements: list[RecruiterPlacementItem] = Field(default_factory=list)


class RecruiterIndexingInput(BaseModel):
    """Input to RecruiterIndexingWorkflow.

    `input_kind` is single-shape today (`"profile"`); kept as a Literal so the
    contract can grow to e.g. `"resume_file"` without breaking callers.
    """

    input_kind: Literal["profile"] = "profile"
    profile: RecruiterProfile
    source: Literal["seed", "onboarding"]


class ComputedMetrics(BaseModel):
    """Output of compute_placement_metrics activity.

    `placements_by_stage` keys are CompanyStage enum values (lowercase strings).
    """

    fill_rate_pct: float | None = None
    avg_days_to_close: int | None = None
    total_placements: int = 0
    placements_by_stage: dict[str, int] = Field(default_factory=dict)


class ResolveRecruiterDuplicatesResult(BaseModel):
    """Output of resolve_recruiter_duplicates activity.

    Wizard pre-creates the recruiter row, so an existing match is the expected path;
    a missing row is treated as a fail-fast condition by the workflow.
    """

    is_duplicate: bool
    existing_recruiter_id: str | None = None
    match_source: Literal["email"] | None = None


class RecruiterIndexingResult(BaseModel):
    """Output of RecruiterIndexingWorkflow — returned to caller."""

    recruiter_id: str
    status: Literal["active", "pending", "failed"]
    credibility_score: float
    source: str
