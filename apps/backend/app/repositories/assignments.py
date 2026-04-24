from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Assignment
from app.repositories.base_repository import BaseRepository


class AssignmentRepository(BaseRepository[Assignment]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Assignment)

    async def get_by_job(self, job_id: UUID) -> list[Assignment]:
        result = await self.session.execute(
            select(Assignment).where(Assignment.job_id == job_id)
        )
        return list(result.scalars().all())

    async def set_operator_confirmed(
        self,
        assignment_id: UUID,
        operator_id: UUID,
    ) -> Assignment | None:
        """Used by HITL #1 — marks assignment operator_confirmed and timestamps it."""
        assignment = await self.get_by_id(assignment_id)
        if not assignment:
            return None
        assignment.status = "operator_confirmed"
        assignment.confirmed_by_operator_id = operator_id
        assignment.confirmed_at = datetime.now(UTC)
        assignment.updated_at = datetime.now(UTC)
        await self.session.flush()
        await self.session.commit()
        return assignment
