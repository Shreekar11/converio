from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Scorecard
from app.repositories.base_repository import BaseRepository


class ScorecardRepository(BaseRepository[Scorecard]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Scorecard)

    async def get_by_key(
        self, job_id: UUID, candidate_id: UUID, rubric_id: UUID
    ) -> Scorecard | None:
        result = await self.session.execute(
            select(Scorecard).where(
                Scorecard.job_id == job_id,
                Scorecard.candidate_id == candidate_id,
                Scorecard.rubric_id == rubric_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_by_key(
        self,
        job_id: UUID,
        candidate_id: UUID,
        rubric_id: UUID,
        **kwargs,
    ) -> Scorecard:
        """Used by Agent 4 — idempotent scorecard persist (safe on Temporal replay)."""
        existing = await self.get_by_key(job_id, candidate_id, rubric_id)
        if existing:
            for k, v in kwargs.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
            await self.session.flush()
            await self.session.commit()
            return existing
        return await self.create(
            job_id=job_id,
            candidate_id=candidate_id,
            rubric_id=rubric_id,
            **kwargs,
        )

    async def get_by_job(self, job_id: UUID) -> list[Scorecard]:
        result = await self.session.execute(
            select(Scorecard).where(Scorecard.job_id == job_id)
        )
        return list(result.scalars().all())
