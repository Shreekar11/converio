"""Hermetic unit tests for role-aware FastAPI auth dependencies.

Covers `get_current_company_user`, `get_current_recruiter`, and
`get_current_active_company_user` from `app.core.auth`. Each test mocks the
repository surface used by the dep — no real DB session is constructed.

We invoke each dependency function directly (not through a FastAPI app)
because the code under test is pure async function logic; FastAPI's DI graph
adds no behavioural surface area worth re-validating here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.core import auth as auth_module
from app.core.auth import (
    CurrentUser,
    get_current_active_company_user,
    get_current_company_user,
    get_current_recruiter,
)
from app.database.models import Company, CompanyUser, Recruiter


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_current_user() -> CurrentUser:
    return CurrentUser(
        id=f"sup-{uuid.uuid4().hex[:8]}",
        email="user@example.test",
        role="user",
    )


def _make_company_user(
    company_id: uuid.UUID | None = None,
    supabase_user_id: str | None = None,
) -> CompanyUser:
    now = datetime.now(UTC)
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id or uuid.uuid4(),
        supabase_user_id=supabase_user_id,
        email="hm@acme.example.com",
        full_name="Jane Doe",
        role="hiring_manager",
        created_at=now,
        updated_at=now,
    )


def _make_company(status_value: str = "active") -> Company:
    now = datetime.now(UTC)
    return Company(
        id=uuid.uuid4(),
        name="Acme",
        stage="seed",
        industry="Fintech",
        company_size_range="11-50",
        founding_year=2010,
        hq_location="SF",
        status=status_value,
        created_at=now,
        updated_at=now,
    )


def _make_recruiter(status_value: str = "active") -> Recruiter:
    now = datetime.now(UTC)
    return Recruiter(
        id=uuid.uuid4(),
        supabase_user_id=f"sup-{uuid.uuid4().hex[:8]}",
        full_name="Rita Recruiter",
        email="rita@example.test",
        domain_expertise=["engineering"],
        total_placements=0,
        at_capacity=False,
        status=status_value,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# get_current_company_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_company_user_success() -> None:
    user = _make_current_user()
    company_user = _make_company_user(supabase_user_id=user.id)
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=company_user),
    ) as mock_lookup:
        result = await get_current_company_user(current_user=user, session=session)

    assert result is company_user
    mock_lookup.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_get_current_company_user_not_found_returns_403() -> None:
    user = _make_current_user()
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_company_user(current_user=user, session=session)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Not a company user"


# ---------------------------------------------------------------------------
# get_current_recruiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_recruiter_success() -> None:
    user = _make_current_user()
    recruiter = _make_recruiter(status_value="active")
    session = AsyncMock()

    with patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=recruiter),
    ) as mock_lookup:
        result = await get_current_recruiter(current_user=user, session=session)

    assert result is recruiter
    mock_lookup.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_get_current_recruiter_not_found_returns_403() -> None:
    user = _make_current_user()
    session = AsyncMock()

    with patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_recruiter(current_user=user, session=session)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Not a recruiter"


@pytest.mark.asyncio
async def test_get_current_recruiter_suspended_returns_403() -> None:
    user = _make_current_user()
    recruiter = _make_recruiter(status_value="suspended")
    session = AsyncMock()

    with patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=recruiter),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_recruiter(current_user=user, session=session)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Recruiter account suspended"


# ---------------------------------------------------------------------------
# get_current_active_company_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_active_company_user_success() -> None:
    company = _make_company(status_value="active")
    company_user = _make_company_user(company_id=company.id)
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ) as mock_lookup:
        result = await get_current_active_company_user(
            company_user=company_user, session=session
        )

    assert result is company_user
    mock_lookup.assert_awaited_once_with(company.id)


@pytest.mark.asyncio
async def test_get_current_active_company_user_pending_review_returns_403() -> None:
    company = _make_company(status_value="pending_review")
    company_user = _make_company_user(company_id=company.id)
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_company_user(
                company_user=company_user, session=session
            )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Company not active"


@pytest.mark.asyncio
async def test_get_current_active_company_user_paused_returns_403() -> None:
    company = _make_company(status_value="paused")
    company_user = _make_company_user(company_id=company.id)
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_company_user(
                company_user=company_user, session=session
            )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Company not active"


@pytest.mark.asyncio
async def test_get_current_active_company_user_company_missing_returns_403() -> None:
    """Defensive — race with company deletion. Not in the original test list
    but required by the implementation contract; without it the missing-row
    branch is uncovered."""
    company_user = _make_company_user()
    session = AsyncMock()

    with patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_company_user(
                company_user=company_user, session=session
            )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == "Company not found"
