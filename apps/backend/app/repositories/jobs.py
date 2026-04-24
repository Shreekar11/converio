from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Job
from app.repositories.base_repository import BaseRepository


class JobRepository(BaseRepository[Job]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Job)

    async def get_with_rubric(self, job_id: UUID) -> Job | None:
        """Used by Agent 1 — loads job + latest rubric in single query."""
        result = await self.session.execute(
            select(Job)
            .options(selectinload(Job.rubrics))
            .where(Job.id == job_id)
        )
        return result.scalar_one_or_none()

    async def get_by_workflow_id(self, workflow_id: str) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.workflow_id == workflow_id)
        )
        return result.scalar_one_or_none()
