
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Operator
from app.repositories.base_repository import BaseRepository


class OperatorRepository(BaseRepository[Operator]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Operator)

    async def get_by_supabase_id(self, supabase_user_id: str) -> Operator | None:
        result = await self.session.execute(
            select(Operator).where(Operator.supabase_user_id == supabase_user_id)
        )
        return result.scalar_one_or_none()
