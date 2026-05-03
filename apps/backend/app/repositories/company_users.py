"""CompanyUser repository — backs operator-only seat-provisioning endpoints.

`POST /companies/{id}/users` and `GET /companies/{id}/users` (Phase B2/B3).
The `email` uniqueness contract is enforced at the application layer here:
the DB has no `UNIQUE` on `company_users.email` so we look it up before
insert and return 409 on conflict.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CompanyUser
from app.repositories.base_repository import BaseRepository


class CompanyUserRepository(BaseRepository[CompanyUser]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, CompanyUser)

    async def get_by_supabase_user_id(
        self, supabase_user_id: str
    ) -> CompanyUser | None:
        """Fetch hiring-manager / admin seat by Supabase auth user id.

        Mirrors `OperatorRepository.get_by_supabase_id` — used by the
        self-serve auth flow to resolve `auth.users.id` -> seated row.
        """
        result = await self.session.execute(
            select(CompanyUser).where(
                CompanyUser.supabase_user_id == supabase_user_id
            )
        )
        return result.scalar_one_or_none()

    async def link_supabase_user_id(
        self, user_id: UUID, supabase_user_id: str
    ) -> CompanyUser | None:
        """Bind a Supabase auth user id to an already-seated CompanyUser row.

        Used on first sign-in: the operator pre-provisioned the seat (no
        `supabase_user_id`), then the user signs in via Supabase and we link
        the auth identity. Issues an `UPDATE` statement scoped to the row id
        and re-fetches via `get_by_id` so the returned row reflects the new
        value (and any DB-side `updated_at` triggers).

        Note: we explicitly call `session.commit()` here because this path
        bypasses `BaseRepository.update`'s ORM-instance mutation pattern —
        we use a Core `update()` statement to avoid loading the row twice
        when only one column changes. Endpoints rely on the repo to own
        commit semantics, so we flush+commit inside the repo and let the
        caller treat the returned model as the post-commit state.
        """
        await self.session.execute(
            update(CompanyUser)
            .where(CompanyUser.id == user_id)
            .values(supabase_user_id=supabase_user_id)
        )
        await self.session.commit()
        return await self.get_by_id(user_id)

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
