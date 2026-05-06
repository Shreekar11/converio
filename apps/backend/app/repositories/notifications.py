import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Notification
from app.repositories.base_repository import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Notification)

    async def create_notification(
        self,
        *,
        recruiter_id: uuid.UUID,
        job_id: uuid.UUID,
        payload: dict,
        channel: str = "stub",
    ) -> Notification:
        notif = Notification(
            recruiter_id=recruiter_id,
            job_id=job_id,
            payload=payload,
            channel=channel,
            status="sent",
        )
        self.session.add(notif)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(notif)
        return notif

    async def list_for_job(self, job_id: uuid.UUID) -> list[Notification]:
        result = await self.session.execute(
            select(Notification)
            .where(Notification.job_id == job_id)
            .order_by(Notification.dispatched_at.desc())
        )
        return list(result.scalars().all())
