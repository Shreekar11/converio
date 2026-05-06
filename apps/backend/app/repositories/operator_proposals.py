import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OperatorProposal
from app.repositories.base_repository import BaseRepository


class OperatorProposalRepository(BaseRepository[OperatorProposal]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, OperatorProposal)

    async def fetch_latest_for_job(
        self, job_id: uuid.UUID
    ) -> OperatorProposal | None:
        """Fetch the most recent non-superseded proposal for a job."""
        result = await self.session.execute(
            select(OperatorProposal)
            .where(
                OperatorProposal.job_id == job_id,
                OperatorProposal.superseded_at.is_(None),
            )
            .order_by(OperatorProposal.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def supersede_existing(self, job_id: uuid.UUID) -> int:
        """Mark all non-superseded proposals for this job as superseded (now).

        Returns the number of rows updated.
        """
        now = datetime.utcnow()
        result = await self.session.execute(
            update(OperatorProposal)
            .where(
                OperatorProposal.job_id == job_id,
                OperatorProposal.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        await self.session.commit()
        return result.rowcount

    async def create_proposal(
        self,
        *,
        job_id: uuid.UUID,
        workflow_id: str,
        payload: dict,
        quality_flag: str = "high",
    ) -> OperatorProposal:
        proposal = OperatorProposal(
            job_id=job_id,
            workflow_id=workflow_id,
            payload=payload,
            quality_flag=quality_flag,
        )
        self.session.add(proposal)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal
