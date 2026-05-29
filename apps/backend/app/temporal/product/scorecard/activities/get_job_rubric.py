"""scorecard.get_job_rubric — load Job + Rubric for scoring.

Read-only DB activity. Hydrates the workflow with the role description
(job_description, intake_notes) and the immutable rubric dimensions
(name, description, weight) that the LLM scores against.

Why this is its own activity:

* Workflows cannot do I/O — same Temporal constraint as
  `get_candidate_profile`.
* The rubric is pinned at scorecard time. Loading it through an
  activity means the event history records the exact dimensions used.
  If the rubric is later edited (a new version inserted by Agent 1),
  the Temporal replay of an in-flight workflow continues to score
  against the original version because the activity result is read
  from history, not the database.
* Schema validation at the boundary. Bad rubric shape (missing weight,
  empty dimensions list) surfaces here as a structured ValueError,
  not as a cryptic prompt-assembly or weighted-average error two
  activities later.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.jobs import JobRepository
from app.repositories.rubrics import RubricRepository
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class GetJobRubricInput(BaseModel):
    """Activity input.

    Both ids arrive as strings (Temporal JSON serialization). We
    re-validate them as UUIDs inside the activity so a malformed id
    raises a precise ValueError rather than a SQL coercion error.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="Job UUID as string.")
    rubric_id: str = Field(..., description="Rubric UUID as string (specific version).")


class GetJobRubricOutput(BaseModel):
    """Activity output.

    `dimensions` is a list of dicts (not Pydantic models) because the
    downstream scoring prompt and weighted-average computation both
    accept loose dicts — keeping the wire format dict-shaped avoids
    forcing every consumer to import a `RubricDimension` schema.
    Each dim is expected to carry at minimum `name` (str) and `weight`
    (float). Optional keys: `description`, `evaluation_guidance`,
    `score_anchors`, `evidence_hints`.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    job_title: str
    job_description: str
    intake_notes: str | None = None
    rubric_id: str
    rubric_version: int = Field(
        ..., ge=1, description="Monotonically increasing rubric version per job."
    )
    dimensions: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description=(
            "Rubric dimensions as raw dicts. Caller is responsible for "
            "rendering / weight-sum normalization."
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _parse_uuid(raw: str, field_name: str) -> UUID:
    """Coerce a string to UUID with a precise error message.

    Same discipline as persist_scorecard — explicit error context is
    worth the boilerplate when the failure surfaces inside a Temporal
    activity log.
    """
    try:
        return UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"get_job_rubric: '{field_name}' is not a valid UUID: {raw!r}"
        ) from exc


def _normalize_dimensions(raw: Any, job_id: str, rubric_id: str) -> list[dict[str, Any]]:
    """Validate that the rubric has at least one well-formed dimension.

    Rubric.dimensions is stored as JSONB so the column type is `list`
    but the runtime shape is operator-controlled. We do NOT cast the
    dicts into a strict Pydantic schema here — `build_scoring_prompt`
    and `compute_overall_match_score` deliberately accept loose dicts
    — but we DO enforce the two invariants the rest of the pipeline
    depends on:

    1. The column is a non-empty list.
    2. Every dim has a non-empty `name` and a numeric `weight`.

    Anything else (missing description, extra keys) flows through
    unchanged so the rubric schema can evolve without breaking this
    activity.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"get_job_rubric: rubric {rubric_id} for job {job_id} has no dimensions"
        )

    cleaned: list[dict[str, Any]] = []
    for idx, dim in enumerate(raw):
        if not isinstance(dim, dict):
            raise ValueError(
                f"get_job_rubric: rubric {rubric_id} dim[{idx}] is not a dict: "
                f"{type(dim).__name__}"
            )
        name = dim.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"get_job_rubric: rubric {rubric_id} dim[{idx}] missing required 'name'"
            )
        weight_raw = dim.get("weight")
        if weight_raw is None:
            raise ValueError(
                f"get_job_rubric: rubric {rubric_id} dim[{idx}] ({name}) missing required 'weight'"
            )
        try:
            float(weight_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"get_job_rubric: rubric {rubric_id} dim[{idx}] ({name}) "
                f"has non-numeric weight: {weight_raw!r}"
            ) from exc
        cleaned.append(dict(dim))
    return cleaned


@ActivityRegistry.register("scorecard", "get_job_rubric")
@activity.defn(name="scorecard.get_job_rubric")
async def get_job_rubric(payload: dict) -> dict:
    """Load Job + Rubric from Postgres.

    Args:
        payload: dict matching `GetJobRubricInput`.

    Returns:
        `GetJobRubricOutput.model_dump(mode="json")`.

    Raises:
        pydantic.ValidationError: on malformed payload.
        ValueError: if either id is malformed, the job is missing, the
            rubric is missing OR belongs to a different job, or the
            rubric has no dimensions.
        SQLAlchemyError: on transport failures (propagated for
            Temporal retry).
    """
    model = GetJobRubricInput.model_validate(payload)

    job_uuid = _parse_uuid(model.job_id, "job_id")
    rubric_uuid = _parse_uuid(model.rubric_id, "rubric_id")

    LOGGER.info(
        "scorecard.get_job_rubric: loading",
        extra={"job_id": model.job_id, "rubric_id": model.rubric_id},
    )

    async with async_session_maker() as session:
        job_repo = JobRepository(session)
        rubric_repo = RubricRepository(session)

        job = await job_repo.get_by_id(job_uuid)
        if job is None:
            raise ValueError(
                f"get_job_rubric: job not found for id={model.job_id}"
            )

        rubric = await rubric_repo.get_by_id(rubric_uuid)
        if rubric is None:
            raise ValueError(
                f"get_job_rubric: rubric not found for id={model.rubric_id}"
            )

    # Belt-and-suspenders: a rubric_id from a *different* job is a
    # workflow precondition violation. We refuse rather than blindly
    # using the rubric — scoring against the wrong job's rubric would
    # silently corrupt every downstream rank.
    if rubric.job_id != job.id:
        raise ValueError(
            f"get_job_rubric: rubric {model.rubric_id} belongs to job "
            f"{rubric.job_id}, not {model.job_id}"
        )

    dimensions = _normalize_dimensions(
        rubric.dimensions, job_id=model.job_id, rubric_id=model.rubric_id
    )

    output = GetJobRubricOutput(
        job_id=str(job.id),
        job_title=job.title,
        job_description=job.jd_text,
        intake_notes=job.intake_notes,
        rubric_id=str(rubric.id),
        rubric_version=rubric.version,
        dimensions=dimensions,
    )

    LOGGER.info(
        "scorecard.get_job_rubric: loaded",
        extra={
            "job_id": model.job_id,
            "rubric_id": model.rubric_id,
            "rubric_version": rubric.version,
            "dim_count": len(dimensions),
        },
    )

    return output.model_dump(mode="json")
