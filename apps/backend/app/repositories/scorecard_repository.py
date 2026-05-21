"""Repository for Agent 4 (Scorecard Generator) persistence.

The Scorecard table is unique on `(job_id, candidate_id, rubric_id)` — the
"pinned-to-rubric-version" design point. A new rubric version creates a
new Scorecard row, preserving history for reeval. Within the same rubric
version, however, we want re-runs of the workflow to overwrite the prior
row in-place rather than accumulate duplicates; that is what `upsert_scorecard`
implements via PostgreSQL `ON CONFLICT ... DO UPDATE` on the unique key.

All writes go through the async SQLAlchemy session pattern used by
`AssignmentRepository` and friends: a single `flush()` + `commit()` per
public method so callers do not have to manage transaction boundaries.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Scorecard
from app.repositories.base_repository import BaseRepository
from app.schemas.product.scorecard import ScorecardOutput


class ScorecardRepository(BaseRepository[Scorecard]):
    """Persistence for `scorecards` rows produced by the Scorecard workflow."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Scorecard)

    async def upsert_scorecard(
        self,
        job_id: UUID,
        candidate_id: UUID,
        rubric_id: UUID,
        overall_match_score: Decimal,
        scorecard_output: ScorecardOutput,
        self_correction_triggered: bool,
        dimensions_rescored: list[str],
        tool_call_count: int,
        total_cost_usd: Decimal,
        submission_id: UUID | None = None,
    ) -> Scorecard:
        """Insert-or-update a scorecard, keyed on (job_id, candidate_id, rubric_id).

        We use PostgreSQL's `ON CONFLICT ... DO UPDATE` (atomic upsert) so
        concurrent workflow runs cannot create duplicate rows under the
        same rubric version. On conflict we overwrite all mutable fields
        and bump `updated_at` explicitly (the model's `onupdate` only fires
        on ORM-level updates, not on raw INSERT...ON CONFLICT).

        Reasons we chose `(job_id, candidate_id, rubric_id)` instead of
        `(job_id, candidate_id)` as the conflict target:
          * Matches the existing `uq_scorecards_job_candidate_rubric` UNIQUE
            constraint on the table — using a different target would fail
            at the database layer.
          * Preserves rubric-version history: scorecards under an older
            rubric remain untouched when a new rubric is published.

        The LLM-emitted `ScorecardOutput` is persisted as JSONB in the
        `dimensions` column via `model_dump(mode="json")`; we serialize at
        the persistence boundary (not inside the workflow) so the
        workflow contract keeps working with typed Pydantic models.
        """
        now = datetime.now(UTC)
        # Serialize the LLM output. mode="json" coerces Decimal/UUID into
        # JSON-safe primitives so JSONB ingestion never raises on exotic
        # types embedded inside ScorecardDimension.citation, etc.
        dimensions_payload: list[dict[str, Any]] = [
            dim.model_dump(mode="json") for dim in scorecard_output.dimensions
        ]

        insert_stmt = pg_insert(Scorecard).values(
            job_id=job_id,
            candidate_id=candidate_id,
            rubric_id=rubric_id,
            submission_id=submission_id,
            overall_match_score=overall_match_score,
            dimensions=dimensions_payload,
            strengths=list(scorecard_output.strengths),
            red_flags=list(scorecard_output.red_flags),
            self_correction_triggered=self_correction_triggered,
            dimensions_rescored=list(dimensions_rescored),
            tool_call_count=tool_call_count,
            total_cost_usd=total_cost_usd,
            updated_at=now,
        )

        # ON CONFLICT target must match an existing UNIQUE constraint —
        # using `index_elements` (not `constraint=`) so this stays robust
        # against constraint-name churn in future migrations.
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["job_id", "candidate_id", "rubric_id"],
            set_={
                "submission_id": insert_stmt.excluded.submission_id,
                "overall_match_score": insert_stmt.excluded.overall_match_score,
                "dimensions": insert_stmt.excluded.dimensions,
                "strengths": insert_stmt.excluded.strengths,
                "red_flags": insert_stmt.excluded.red_flags,
                "self_correction_triggered": insert_stmt.excluded.self_correction_triggered,
                "dimensions_rescored": insert_stmt.excluded.dimensions_rescored,
                "tool_call_count": insert_stmt.excluded.tool_call_count,
                "total_cost_usd": insert_stmt.excluded.total_cost_usd,
                "updated_at": now,
            },
        ).returning(Scorecard.id)

        result = await self.session.execute(upsert_stmt)
        scorecard_id = result.scalar_one()
        await self.session.commit()

        # Re-load via the ORM so the caller receives a fully hydrated
        # instance (with relationships available for downstream code) and
        # so the returned object reflects whatever the DB ultimately
        # persisted (server defaults, trigger-modified columns, etc.).
        loaded = await self.get_by_id(scorecard_id)
        if loaded is None:  # pragma: no cover — would imply concurrent delete
            raise RuntimeError(
                f"Scorecard {scorecard_id} disappeared between upsert and reload"
            )
        return loaded

    async def get_by_job_candidate(
        self, job_id: UUID, candidate_id: UUID
    ) -> Scorecard | None:
        """Fetch the most recent scorecard for a (job, candidate) pair.

        Multiple rows may exist if the rubric has been re-versioned over
        time; we return the most-recently-updated row so callers always
        see the current rubric's scorecard without having to know the
        active rubric id.
        """
        result = await self.session.execute(
            select(Scorecard)
            .where(Scorecard.job_id == job_id)
            .where(Scorecard.candidate_id == candidate_id)
            .order_by(Scorecard.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_job(self, job_id: UUID) -> list[Scorecard]:
        """List all scorecards for a job, newest-first by `updated_at`.

        Used by the shortlisting UI to rank candidates by score and by the
        Company Review HITL to render the candidate panel.
        """
        result = await self.session.execute(
            select(Scorecard)
            .where(Scorecard.job_id == job_id)
            .order_by(Scorecard.updated_at.desc())
        )
        return list(result.scalars().all())
