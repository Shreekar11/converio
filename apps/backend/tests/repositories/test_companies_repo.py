"""Repository-level tests for `CompanyRepository`.

Covers the `update_status` path added in T1.3 for the self-serve auth
flow's pending -> active transition.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.database.models import Company
from app.repositories.companies import CompanyRepository


def _make_company(*, status_value: str = "pending") -> Company:
    now = datetime.now(UTC)
    return Company(
        id=uuid.uuid4(),
        name="Acme",
        stage="seed",
        status=status_value,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_update_status_returns_updated_company() -> None:
    company = _make_company(status_value="active")
    session = AsyncMock()
    update_result = MagicMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = company
    session.execute.side_effect = [update_result, select_result]

    repo = CompanyRepository(session)
    returned = await repo.update_status(company.id, "active")

    assert returned is company
    assert returned.status == "active"
    assert session.execute.await_count == 2  # UPDATE + SELECT-by-id
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_status_raises_404_when_company_not_found() -> None:
    session = AsyncMock()
    update_result = MagicMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    session.execute.side_effect = [update_result, select_result]

    repo = CompanyRepository(session)

    with pytest.raises(HTTPException) as excinfo:
        await repo.update_status(uuid.uuid4(), "active")

    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert excinfo.value.detail == "Company not found"
    # Commit still fires before the missing-row check — the UPDATE is a no-op
    # at the DB level, so this is safe.
    session.commit.assert_awaited_once()
