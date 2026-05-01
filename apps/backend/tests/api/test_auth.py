"""Hermetic tests for `app.core.auth.get_current_operator`.

The dependency is the public seam exercised by every operator-only endpoint
(Phase B + E). We mock the repository's DB call so these tests run without
PG and assert behaviour through the dependency, not through any private
helper.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.core.auth import CurrentUser, get_current_operator
from app.database.models import Operator


def _make_current_user(user_id: str | None = None) -> CurrentUser:
    """Return a `CurrentUser` mirroring what `get_current_user` would produce
    after a successful Supabase JWT verification. The id is the only field
    `get_current_operator` actually consumes.
    """
    return CurrentUser(
        id=user_id or f"supabase-user-{uuid.uuid4().hex[:8]}",
        email="op@converio.test",
        role="user",
    )


def _make_operator(*, status_value: str, supabase_user_id: str) -> Operator:
    """Return a detached `Operator` ORM instance — never persisted, no
    session attached. `get_current_operator` only reads attributes, so a
    plain instance is sufficient.
    """
    return Operator(
        id=uuid.uuid4(),
        supabase_user_id=supabase_user_id,
        email="op@converio.test",
        full_name="Test Operator",
        status=status_value,
    )


@pytest.fixture
def mock_session():
    """A bare AsyncMock standing in for `AsyncSession`. The real session is
    never touched because `OperatorRepository.get_by_supabase_id` is
    patched at the module boundary.
    """
    return AsyncMock()


async def test_get_current_operator_returns_active_operator(mock_session):
    user = _make_current_user()
    operator = _make_operator(status_value="active", supabase_user_id=user.id)

    with patch(
        "app.core.auth.OperatorRepository.get_by_supabase_id",
        new=AsyncMock(return_value=operator),
    ) as mocked:
        result = await get_current_operator(current_user=user, session=mock_session)

    assert result is operator
    mocked.assert_awaited_once_with(user.id)


async def test_get_current_operator_raises_when_no_row(mock_session):
    user = _make_current_user()

    with patch(
        "app.core.auth.OperatorRepository.get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await get_current_operator(current_user=user, session=mock_session)

    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    # Generic detail — must not leak whether the row is missing vs inactive.
    assert excinfo.value.detail == "Operator privileges required"


async def test_get_current_operator_raises_when_inactive(mock_session):
    user = _make_current_user()
    operator = _make_operator(status_value="inactive", supabase_user_id=user.id)

    with patch(
        "app.core.auth.OperatorRepository.get_by_supabase_id",
        new=AsyncMock(return_value=operator),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await get_current_operator(current_user=user, session=mock_session)

    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Operator privileges required"
