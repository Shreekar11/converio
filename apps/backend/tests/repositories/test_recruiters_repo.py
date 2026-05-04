"""Repository-level tests for `RecruiterRepository`.

Covers the new Supabase lookup and explicit-signature `create` added in
T1.3 for the self-serve recruiter onboarding flow.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import Recruiter
from app.repositories.recruiters import RecruiterRepository


def _make_recruiter(
    *,
    supabase_user_id: str | None = "sup-r-1",
    email: str = "rec@example.com",
) -> Recruiter:
    now = datetime.now(UTC)
    return Recruiter(
        id=uuid.uuid4(),
        supabase_user_id=supabase_user_id,
        full_name="Rita Recruiter",
        email=email,
        domain_expertise=["engineering"],
        status="pending",
        total_placements=0,
        at_capacity=False,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# get_by_supabase_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_supabase_id_returns_match() -> None:
    recruiter = _make_recruiter(supabase_user_id="sup-r-1")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = recruiter
    session.execute.return_value = result

    repo = RecruiterRepository(session)
    found = await repo.get_by_supabase_id("sup-r-1")

    assert found is recruiter
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_supabase_id_returns_none_when_missing() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    repo = RecruiterRepository(session)
    found = await repo.get_by_supabase_id("sup-missing")

    assert found is None


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_full_fields_persists_and_commits() -> None:
    session = AsyncMock()
    # `Session.add` is sync; AsyncMock would otherwise return an unawaited coroutine.
    session.add = MagicMock()
    repo = RecruiterRepository(session)

    created = await repo.create(
        supabase_user_id="sup-r-2",
        email="full@example.com",
        full_name="Full Fields",
        domain_expertise=["engineering", "gtm"],
        workspace_type="agency",
        recruited_funding_stage="series_a",
        bio="10y agency recruiter",
        linkedin_url="https://linkedin.com/in/full",
        status="active",
    )

    assert isinstance(created, Recruiter)
    assert created.email == "full@example.com"
    assert created.full_name == "Full Fields"
    assert created.domain_expertise == ["engineering", "gtm"]
    assert created.workspace_type == "agency"
    assert created.recruited_funding_stage == "series_a"
    assert created.bio == "10y agency recruiter"
    assert created.linkedin_url == "https://linkedin.com/in/full"
    assert created.status == "active"
    session.add.assert_called_once_with(created)
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(created)


@pytest.mark.asyncio
async def test_create_with_only_required_fields_defaults_optionals() -> None:
    session = AsyncMock()
    # `Session.add` is sync; AsyncMock would otherwise return an unawaited coroutine.
    session.add = MagicMock()
    repo = RecruiterRepository(session)

    created = await repo.create(
        supabase_user_id="sup-r-3",
        email="min@example.com",
        full_name="Min Fields",
        domain_expertise=["sales"],
    )

    assert created.email == "min@example.com"
    assert created.domain_expertise == ["sales"]
    # Optionals default to None.
    assert created.workspace_type is None
    assert created.recruited_funding_stage is None
    assert created.bio is None
    assert created.linkedin_url is None
    # Status defaults to "pending".
    assert created.status == "pending"
    session.commit.assert_awaited_once()
