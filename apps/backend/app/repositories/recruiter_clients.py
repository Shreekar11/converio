from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RecruiterClient
from app.repositories.base_repository import BaseRepository


class RecruiterClientRepository(BaseRepository[RecruiterClient]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RecruiterClient)

    async def get_by_recruiter(self, recruiter_id: UUID) -> list[RecruiterClient]:
        """Used by Agent 0 — past clients feed recruiter fit scoring."""
        result = await self.session.execute(
            select(RecruiterClient).where(RecruiterClient.recruiter_id == recruiter_id)
        )
        return list(result.scalars().all())
