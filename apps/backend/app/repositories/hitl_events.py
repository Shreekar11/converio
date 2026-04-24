from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import HitlEvent
from app.repositories.base_repository import BaseRepository


class HitlEventRepository(BaseRepository[HitlEvent]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, HitlEvent)

    async def get_by_job(self, job_id: UUID) -> list[HitlEvent]:
        result = await self.session.execute(
            select(HitlEvent).where(HitlEvent.job_id == job_id)
        )
        return list(result.scalars().all())
