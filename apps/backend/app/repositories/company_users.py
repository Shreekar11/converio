"""CompanyUser repository — backs operator-only seat-provisioning endpoints.

`POST /companies/{id}/users` and `GET /companies/{id}/users` (Phase B2/B3).
The `email` uniqueness contract is enforced at the application layer here:
the DB has no `UNIQUE` on `company_users.email` so we look it up before
insert and return 409 on conflict.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CompanyUser
from app.repositories.base_repository import BaseRepository


class CompanyUserRepository(BaseRepository[CompanyUser]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, CompanyUser)

    async def get_by_email(self, email: str) -> CompanyUser | None:
        """Lookup a seated user by email (case-sensitive — email validation
        normalizes via Pydantic `EmailStr` upstream).

        Used as a duplicate-seat guard for `POST /companies/{id}/users`.
        """
        result = await self.session.execute(
            select(CompanyUser).where(CompanyUser.email == email)
        )
        return result.scalar_one_or_none()

    async def list_for_company(self, company_id: UUID) -> list[CompanyUser]:
        """Return seated hiring-manager / admin users for the given company.

        Ordered by `created_at DESC` so the most recent seat provisioning
        shows first in the operator console.
        """
        result = await self.session.execute(
            select(CompanyUser)
            .where(CompanyUser.company_id == company_id)
            .order_by(CompanyUser.created_at.desc())
        )
        return list(result.scalars().all())
