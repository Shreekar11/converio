from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RecruiterPlacement
from app.repositories.base_repository import BaseRepository


class RecruiterPlacementRepository(BaseRepository[RecruiterPlacement]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RecruiterPlacement)

    async def get_by_recruiter(self, recruiter_id: UUID) -> list[RecruiterPlacement]:
        """Used by Agent 0 — placement history feeds Neo4j PLACED_AT edges + fit scoring."""
        result = await self.session.execute(
            select(RecruiterPlacement).where(
                RecruiterPlacement.recruiter_id == recruiter_id
            )
        )
        return list(result.scalars().all())
