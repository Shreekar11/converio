
from sqlalchemy import any_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Recruiter
from app.repositories.base_repository import BaseRepository


class RecruiterRepository(BaseRepository[Recruiter]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Recruiter)

    async def get_by_email(self, email: str) -> Recruiter | None:
        result = await self.session.execute(
            select(Recruiter).where(Recruiter.email == email)
        )
        return result.scalar_one_or_none()

    async def search_by_domain(self, domain: str) -> list[Recruiter]:
        """Fallback PG filter — Neo4j Cypher is primary for recruiter search (Agent 0).

        Uses `= ANY(domain_expertise)` to check array element membership.
        """
        result = await self.session.execute(
            select(Recruiter).where(
                domain == any_(Recruiter.domain_expertise),
                Recruiter.status == "active",
                Recruiter.at_capacity.is_(False),
            )
        )
        return list(result.scalars().all())
