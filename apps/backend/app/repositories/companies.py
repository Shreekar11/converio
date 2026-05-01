"""Company repository — extends `BaseRepository[Company]` with helpers
needed by the operator-only company onboarding endpoints (Phase B).

Helpers exposed:
- `get_by_name_ci`  — case-insensitive duplicate-name guard for `POST /companies`.
- `list_paginated`  — `(rows, total)` projection for `GET /companies`.
- `get_with_users`  — eager-loads `Company.users` for `GET /companies/{id}`.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Company
from app.repositories.base_repository import BaseRepository


class CompanyRepository(BaseRepository[Company]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Company)

    async def get_by_name_ci(self, name: str) -> Company | None:
        """Case-insensitive lookup by company name.

        Used by `POST /companies` to enforce the (application-layer) uniqueness
        contract documented in the OpenAPI spec — DB does not currently carry
        a unique constraint on `companies.name`.
        """
        result = await self.session.execute(
            select(Company).where(func.lower(Company.name) == name.lower())
        )
        return result.scalar_one_or_none()

    async def list_paginated(
        self, *, limit: int, offset: int
    ) -> tuple[list[Company], int]:
        """Return `(rows, total_count)` for paginated listing.

        Two queries (count + page) keep the contract simple and let the
        endpoint echo `total` back in the `CompaniesListResponse` envelope.
        Ordered by `created_at DESC` so the most recently onboarded clients
        appear first in the operator console.
        """
        total_result = await self.session.execute(
            select(func.count()).select_from(Company)
        )
        total = total_result.scalar_one()

        rows_result = await self.session.execute(
            select(Company)
            .order_by(Company.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = rows_result.scalars().all()
        return list(rows), int(total)

    async def get_with_users(self, company_id: UUID) -> Company | None:
        """Fetch a company by id with its `users` relationship eager-loaded.

        Used by `GET /companies/{id}` so the detail endpoint can render the
        seated hiring-manager / admin list in a single round-trip without
        triggering lazy-load IO on serialization.
        """
        result = await self.session.execute(
            select(Company)
            .options(selectinload(Company.users))
            .where(Company.id == company_id)
        )
        return result.scalar_one_or_none()
