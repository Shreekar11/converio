from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Rubric
from app.repositories.base_repository import BaseRepository


class RubricRepository(BaseRepository[Rubric]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Rubric)

    async def get_latest_for_job(self, job_id: UUID) -> Rubric | None:
        result = await self.session.execute(
            select(Rubric)
            .where(Rubric.job_id == job_id)
            .order_by(Rubric.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
