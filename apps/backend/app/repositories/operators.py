
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Operator
from app.repositories.base_repository import BaseRepository


class OperatorRepository(BaseRepository[Operator]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Operator)

    async def get_by_supabase_id(self, supabase_user_id: str) -> Operator | None:
        """Fetch operator by Supabase auth user id."""
        result = await self.session.execute(
            select(Operator).where(Operator.supabase_user_id == supabase_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Operator | None:
        """Fetch operator by email.

        Used by self-serve signup flows to enforce cross-role email uniqueness:
        an email already provisioned as an operator cannot be reused for a
        company user or recruiter signup.
        """
        result = await self.session.execute(
            select(Operator).where(Operator.email == email)
        )
        return result.scalar_one_or_none()
