from __future__ import annotations

from sqlalchemy import any_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Recruiter
from app.repositories.base_repository import BaseRepository


class RecruiterRepository(BaseRepository[Recruiter]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Recruiter)

    async def get_by_supabase_id(
        self, supabase_user_id: str
    ) -> Recruiter | None:
        """Fetch recruiter by Supabase auth user id.

        Mirrors `OperatorRepository.get_by_supabase_id` — used by the
        self-serve auth flow once a recruiter has linked their auth identity.
        """
        result = await self.session.execute(
            select(Recruiter).where(Recruiter.supabase_user_id == supabase_user_id)
        )
        return result.scalar_one_or_none()

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

    async def create(  # type: ignore[override]
        self,
        *,
        supabase_user_id: str,
        email: str,
        full_name: str,
        domain_expertise: list[str],
        workspace_type: str | None = None,
        recruited_funding_stage: str | None = None,
        bio: str | None = None,
        linkedin_url: str | None = None,
        status: str = "pending",
    ) -> Recruiter:
        """Insert a new Recruiter row from the self-serve onboarding wizard.

        Owns commit semantics so callers don't need to touch `session.commit()`.
        Mirrors `BaseRepository.create` (add -> flush -> commit) but with an
        explicit, typed signature so endpoint code doesn't pass arbitrary
        kwargs into the ORM model.
        """
        recruiter = Recruiter(
            supabase_user_id=supabase_user_id,
            email=email,
            full_name=full_name,
            domain_expertise=domain_expertise,
            workspace_type=workspace_type,
            recruited_funding_stage=recruited_funding_stage,
            bio=bio,
            linkedin_url=linkedin_url,
            status=status,
        )
        self.session.add(recruiter)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(recruiter)
        return recruiter
