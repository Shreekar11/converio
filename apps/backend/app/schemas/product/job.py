"""Pydantic models for the Job Intake workflow IO.

Hand-rolled (vs. the generated `app.schemas.generated.jobs` models) because:
- Workflow inputs are passed through Temporal as JSON; field shape is
  workflow-internal and decoupled from the HTTP request shape.
- Outputs include LLM structured-output schemas (RoleClassification,
  EvaluationRubric) that need replay-deterministic post-validation
  (sort + dedupe of skill lists, weight bounds, dimension count).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.enums import (
    CompanyStage,
    JobStatus,
    RemoteOnsite,
    RoleCategory,
    Seniority,
)

_MAX_SKILLS = 20
_MIN_DIMENSIONS = 4
_MAX_DIMENSIONS = 8


def _normalize_skill_list(values: list[str]) -> list[str]:
    """Lower-case, strip, dedupe, sort a list of skill strings.

    Replay-determinism guard: LLMs return skill arrays in arbitrary order;
    Temporal replay equality requires deterministic ordering at the
    activity boundary. Empty strings (post-strip) are rejected because the
    LLM occasionally emits whitespace-only entries that would otherwise
    survive dedupe.
    """
    cleaned: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"skill entries must be strings, got {type(raw).__name__}")
        normalized = raw.strip().lower()
        if not normalized:
            raise ValueError("skill entries must not be empty after stripping whitespace")
        cleaned.add(normalized)
    return sorted(cleaned)


class JobIntakeInput(BaseModel):
    """Payload the workflow receives from the API on `start_workflow`.

    Decoupled from the HTTP `JobIntakeRequest` schema (in
    `app.schemas.generated.jobs`) so the workflow contract can evolve
    independently of the public API.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="UUID of the pre-inserted Job row.")
    title: str = Field(..., min_length=1, max_length=200)
    jd_text: str = Field(..., min_length=1, max_length=20000)
    intake_notes: str | None = None
    remote_onsite: RemoteOnsite | None = None
    location_text: str | None = None
    compensation_min: int | None = Field(default=None, ge=0)
    compensation_max: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_compensation_order(self) -> "JobIntakeInput":
        if (
            self.compensation_min is not None
            and self.compensation_max is not None
            and self.compensation_max < self.compensation_min
        ):
            raise ValueError(
                "compensation_max must be greater than or equal to compensation_min"
            )
        return self


class RoleClassification(BaseModel):
    """Output of `classify_role_type` activity.

    Doubles as the structured-output schema for the LLM call. Skill lists
    are normalized (lower + strip + dedupe + sort) post-validation so
    Temporal replays produce identical event payloads.
    """

    model_config = ConfigDict(extra="forbid")

    role_category: RoleCategory
    seniority_level: Seniority
    stage_fit: CompanyStage | None = None
    remote_onsite: RemoteOnsite | None = None
    must_have_skills: list[str] = Field(default_factory=list, max_length=_MAX_SKILLS)
    nice_to_have_skills: list[str] = Field(default_factory=list, max_length=_MAX_SKILLS)
    rationale: str = Field(..., min_length=1, max_length=1000)

    @field_validator("must_have_skills", "nice_to_have_skills", mode="before")
    @classmethod
    def _normalize_skills(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("skill fields must be a list of strings")
        return _normalize_skill_list(value)


class RubricDimension(BaseModel):
    """One weighted dimension of an `EvaluationRubric`.

    `name` is constrained to lowercase_snake_case so downstream agents can
    use it as a dictionary key without further normalization.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="lowercase_snake_case identifier; used as a stable key downstream.",
    )
    description: str = Field(..., min_length=1, max_length=500)
    weight: float = Field(..., ge=0.0, le=1.0)
    evaluation_guidance: str = Field(..., min_length=1, max_length=1000)


class EvaluationRubric(BaseModel):
    """Output of `generate_evaluation_rubric` activity.

    The LLM is prompted to return between `_MIN_DIMENSIONS` and
    `_MAX_DIMENSIONS` dimensions; the activity normalizes weights and
    truncates before model construction. Uniqueness of `name` is enforced
    here (not at the LLM prompt level) because the prompt cannot be
    trusted to produce distinct keys on every replay.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[RubricDimension] = Field(
        ...,
        min_length=_MIN_DIMENSIONS,
        max_length=_MAX_DIMENSIONS,
    )
    rationale: str = Field(..., min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_dimensions(self) -> "EvaluationRubric":
        if not (_MIN_DIMENSIONS <= len(self.dimensions) <= _MAX_DIMENSIONS):
            raise ValueError(
                f"rubric must have between {_MIN_DIMENSIONS} and "
                f"{_MAX_DIMENSIONS} dimensions, got {len(self.dimensions)}"
            )
        names = [dim.name for dim in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError("rubric dimension names must be unique")
        return self


class JobIntakeResult(BaseModel):
    """Output of `JobIntakeWorkflow.run`.

    `status` is intentionally typed as the open `JobStatus` enum (not a
    `Literal["recruiter_assignment"]`) so future PRs can extend the
    workflow past `persist_job_record` without breaking this contract.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    rubric_id: str
    rubric_version: int = Field(..., ge=1)
    status: JobStatus
