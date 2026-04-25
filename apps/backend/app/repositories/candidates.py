
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Candidate
from app.repositories.base_repository import BaseRepository


class CandidateRepository(BaseRepository[Candidate]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Candidate)

    async def get_by_dedup_hash(self, dedup_hash: str) -> Candidate | None:
        """Used by Agent 2 resolve_entity_duplicates to prevent duplicate indexing."""
        result = await self.session.execute(
            select(Candidate).where(Candidate.dedup_hash == dedup_hash)
        )
        return result.scalar_one_or_none()

    async def get_by_github_username(self, github_username: str) -> Candidate | None:
        """Used by Agent 2 resolve_entity_duplicates — GitHub username dedup path."""
        result = await self.session.execute(
            select(Candidate).where(Candidate.github_username == github_username)
        )
        return result.scalar_one_or_none()
