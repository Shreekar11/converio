
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import WorkflowRun
from app.repositories.base_repository import BaseRepository


class WorkflowRunRepository(BaseRepository[WorkflowRun]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, WorkflowRun)

    async def get_by_workflow_id(self, workflow_id: str) -> WorkflowRun | None:
        result = await self.session.execute(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        )
        return result.scalar_one_or_none()
