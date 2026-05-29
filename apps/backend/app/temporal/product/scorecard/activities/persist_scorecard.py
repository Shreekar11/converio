"""scorecard.persist_scorecard — idempotent upsert into the `scorecards` table.

Final write step of the Scorecard workflow. Persists the LLM-emitted
`ScorecardOutput` plus the deterministically-computed
`overall_match_score` plus self-correction metadata under the unique
key `(job_id, candidate_id, rubric_id)`.

Idempotency contract
--------------------
* The repository uses PostgreSQL `INSERT ... ON CONFLICT DO UPDATE` on
  the `(job_id, candidate_id, rubric_id)` unique index. Re-running this
  activity (Temporal retry, replay, or workflow re-trigger under the
  same rubric version) overwrites in-place rather than creating
  duplicate rows.
* `updated_at` is set to `now()` on every write (insert AND conflict
  update) inside the repository — the model's `onupdate` only fires on
  ORM-level updates, not on raw INSERT...ON CONFLICT, so the repo sets
  it explicitly.

Decimal serialization
---------------------
* `overall_match_score` and `total_cost_usd` cross the activity
  boundary as JSON strings to preserve bit-exactness (JSON has no
  native `Decimal` type and `float` would re-introduce IEEE-754 noise).
  We parse them back into `Decimal` here using `Decimal(str)` — same
  discipline as `compute_overall_match_score`.

Schema validation
-----------------
* `scorecard_output` is re-validated through `ScorecardOutput.model_validate`
  inside this activity. The contract is: callers may pass a dict, we
  raise `ValidationError` if it doesn't conform. This catches schema
  drift between Phase 4 (LLM output) and Phase 1 (storage model) at
  the boundary rather than as a cryptic SQLAlchemy error.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.core.database import async_session_maker
from app.repositories.scorecard_repository import ScorecardRepository
from app.schemas.product.scorecard import ScorecardOutput
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class PersistScorecardInput(BaseModel):
    """Activity input.

    Decimal fields arrive as strings because:
      * JSON has no native `Decimal` type — Temporal serializes via
        JSON, so passing a `Decimal` directly would lose precision.
      * Strings round-trip bit-exactly through `Decimal(str(...))`.

    `submission_id` is optional (nullable in DB schema) — `None` for
    sourcing-agent candidates that did not come from a recruiter
    submission.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="Job UUID as string.")
    candidate_id: str = Field(..., description="Candidate UUID as string.")
    rubric_id: str = Field(..., description="Rubric UUID as string.")
    submission_id: str | None = Field(
        default=None,
        description="Candidate submission UUID as string; None for sourcing-agent candidates.",
    )
    overall_match_score: str = Field(
        ...,
        description="Decimal weighted average serialized as string (e.g. '87.42').",
    )
    scorecard_output: dict[str, Any] = Field(
        ...,
        description="`ScorecardOutput.model_dump(mode=\"json\")` payload from the workflow.",
    )
    self_correction_triggered: bool = Field(
        ..., description="True iff the self-correction loop ran ≥1 iteration."
    )
    dimensions_rescored: list[str] = Field(
        default_factory=list,
        description="Names of dimensions that were re-scored during self-correction.",
    )
    tool_call_count: int = Field(
        ..., ge=0, description="Total tool calls consumed (LLM + fetchers)."
    )
    total_cost_usd: str = Field(
        ..., description="Decimal USD cost serialized as string (e.g. '0.1834')."
    )


class PersistScorecardOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorecard_id: str = Field(..., description="Newly inserted or upserted scorecard UUID.")
    updated_at: str = Field(..., description="ISO-8601 timestamp of the upsert.")


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _parse_uuid(raw: str, field_name: str) -> UUID:
    """Coerce a string to UUID with a precise error message.

    We do this here (rather than relying on pydantic's UUID coercion in
    the input model) so the field name appears in the error — the
    default pydantic message is harder to correlate when persistence
    fails inside a Temporal activity.
    """
    try:
        return UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"persist_scorecard: '{field_name}' is not a valid UUID: {raw!r}"
        ) from exc


def _parse_decimal(raw: str, field_name: str) -> Decimal:
    """Coerce a string to Decimal with a precise error message.

    `Decimal(str)` is the bit-exact-safe coercion; we never accept
    floats here, even though pydantic would happily round-trip them.
    """
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"persist_scorecard: '{field_name}' is not a valid Decimal: {raw!r}"
        ) from exc


@ActivityRegistry.register("scorecard", "persist_scorecard")
@activity.defn(name="scorecard.persist_scorecard")
async def persist_scorecard(payload: dict) -> dict:
    """Upsert a scorecard row keyed on (job_id, candidate_id, rubric_id).

    Args:
        payload: dict matching `PersistScorecardInput`.

    Returns:
        dict matching `PersistScorecardOutput`.

    Raises:
        pydantic.ValidationError: on malformed `payload` shape.
        ValueError: on malformed UUID/Decimal field values or malformed
            `scorecard_output`.
        SQLAlchemyError: on database failure — propagated so Temporal's
            activity retry policy can handle transient PG blips. The
            upsert is idempotent so retries are safe.
    """
    model = PersistScorecardInput.model_validate(payload)

    job_uuid = _parse_uuid(model.job_id, "job_id")
    candidate_uuid = _parse_uuid(model.candidate_id, "candidate_id")
    rubric_uuid = _parse_uuid(model.rubric_id, "rubric_id")
    submission_uuid: UUID | None = (
        _parse_uuid(model.submission_id, "submission_id")
        if model.submission_id is not None
        else None
    )

    overall_match_score = _parse_decimal(
        model.overall_match_score, "overall_match_score"
    )
    total_cost_usd = _parse_decimal(model.total_cost_usd, "total_cost_usd")

    # Re-validate the LLM-emitted scorecard payload at the persistence
    # boundary. Schema drift between Phase 4 (LLM output) and Phase 1
    # (DB schema) MUST surface here as a clean validation error, not
    # as a cryptic JSONB ingestion error halfway through commit.
    scorecard_output = ScorecardOutput.model_validate(model.scorecard_output)

    LOGGER.info(
        "Persisting scorecard",
        extra={
            "job_id": model.job_id,
            "candidate_id": model.candidate_id,
            "rubric_id": model.rubric_id,
            "submission_id": model.submission_id,
            "overall_match_score": str(overall_match_score),
            "self_correction_triggered": model.self_correction_triggered,
            "dimensions_rescored_count": len(model.dimensions_rescored),
            "tool_call_count": model.tool_call_count,
        },
    )

    async with async_session_maker() as session:
        repo = ScorecardRepository(session)
        scorecard = await repo.upsert_scorecard(
            job_id=job_uuid,
            candidate_id=candidate_uuid,
            rubric_id=rubric_uuid,
            overall_match_score=overall_match_score,
            scorecard_output=scorecard_output,
            self_correction_triggered=model.self_correction_triggered,
            dimensions_rescored=model.dimensions_rescored,
            tool_call_count=model.tool_call_count,
            total_cost_usd=total_cost_usd,
            submission_id=submission_uuid,
        )

    # `scorecard.updated_at` is a tz-aware TIMESTAMPTZ; serialize via
    # `.isoformat()` for transport. If the underlying column is naive
    # for any reason (shouldn't happen — server_default is `NOW()`
    # which is tz-aware), fall back to `datetime.now(UTC)` so the
    # workflow always sees a tz-aware ISO string.
    updated_at = scorecard.updated_at or datetime.now(UTC)
    updated_at_iso = updated_at.isoformat()

    LOGGER.info(
        "Scorecard persisted",
        extra={
            "scorecard_id": str(scorecard.id),
            "job_id": model.job_id,
            "candidate_id": model.candidate_id,
            "rubric_id": model.rubric_id,
            "updated_at": updated_at_iso,
        },
    )

    return PersistScorecardOutput(
        scorecard_id=str(scorecard.id),
        updated_at=updated_at_iso,
    ).model_dump(mode="json")
