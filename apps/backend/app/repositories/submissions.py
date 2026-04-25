from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CandidateSubmission
from app.repositories.base_repository import BaseRepository


class CandidateSubmissionRepository(BaseRepository[CandidateSubmission]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, CandidateSubmission)

    async def get_by_job_candidate(
        self, job_id: UUID, candidate_id: UUID
    ) -> CandidateSubmission | None:
        result = await self.session.execute(
            select(CandidateSubmission).where(
                CandidateSubmission.job_id == job_id,
                CandidateSubmission.candidate_id == candidate_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_job(self, job_id: UUID) -> list[CandidateSubmission]:
        result = await self.session.execute(
            select(CandidateSubmission).where(CandidateSubmission.job_id == job_id)
        )
        return list(result.scalars().all())
