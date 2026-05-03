"""Repository-level tests for `CompanyUserRepository`.

Covers the new self-serve auth lookups and link path added in T1.3.
SQLAlchemy session is mocked — these tests assert behaviour at the
repository boundary (queries issued, return values, commit semantics)
without round-tripping a real PG.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import CompanyUser
from app.repositories.company_users import CompanyUserRepository


def _make_company_user(
    *,
    supabase_user_id: str | None = None,
    email: str = "hm@acme.example.com",
) -> CompanyUser:
    now = datetime.now(UTC)
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        supabase_user_id=supabase_user_id,
        email=email,
        full_name="Jane Doe",
        role="hiring_manager",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# get_by_supabase_user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_supabase_user_id_returns_match() -> None:
    user = _make_company_user(supabase_user_id="sup-123")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session.execute.return_value = result

    repo = CompanyUserRepository(session)
    found = await repo.get_by_supabase_user_id("sup-123")

    assert found is user
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_supabase_user_id_returns_none_when_missing() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    repo = CompanyUserRepository(session)
    found = await repo.get_by_supabase_user_id("sup-missing")

    assert found is None
    session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# link_supabase_user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_supabase_user_id_updates_and_returns_row() -> None:
    user_id = uuid.uuid4()
    linked = _make_company_user(supabase_user_id="sup-new")
    linked.id = user_id

    session = AsyncMock()
    # First execute() = the UPDATE; second = the get_by_id SELECT.
    update_result = MagicMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = linked
    session.execute.side_effect = [update_result, select_result]

    repo = CompanyUserRepository(session)
    returned = await repo.link_supabase_user_id(user_id, "sup-new")

    assert returned is linked
    assert returned.supabase_user_id == "sup-new"
    # Two executes: UPDATE + SELECT-by-id.
    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_supabase_user_id_returns_none_when_row_missing() -> None:
    """If `get_by_id` finds nothing post-update, the helper returns None.

    Endpoints that depend on a hit must convert this to an HTTP error;
    the repo itself stays generic.
    """
    session = AsyncMock()
    update_result = MagicMock()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    session.execute.side_effect = [update_result, select_result]

    repo = CompanyUserRepository(session)
    returned = await repo.link_supabase_user_id(uuid.uuid4(), "sup-x")

    assert returned is None
    session.commit.assert_awaited_once()
