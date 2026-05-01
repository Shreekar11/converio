"""Hermetic endpoint tests for the operator-only company onboarding API.

These tests exercise the public HTTP surface of `POST /companies`,
`GET /companies`, and `GET /companies/{id}` per `docs/plans/job_intake_plan.md`
phase B5.

Strategy (per `.claude/rules/test-driven-development.mdc`):
- The DB session and repository methods are mocked. Tests assert
  observable behaviour (status code, payload, dependency invocations) —
  never internals.
- A minimal FastAPI app mounts only the companies router so we bypass
  the JWT middleware on the production app. `get_current_operator` and
  `get_async_session` are overridden via FastAPI's dependency override
  mechanism.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import companies as companies_module
from app.core.auth import get_current_operator
from app.core.database import get_async_session
from app.database.models import Company, CompanyUser, Operator


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    return Operator(
        id=uuid.uuid4(),
        supabase_user_id=f"sup-{uuid.uuid4().hex[:8]}",
        email="op@converio.test",
        full_name="Test Operator",
        status="active",
    )


def _make_company(name: str = "Acme") -> Company:
    now = datetime.now(UTC)
    return Company(
        id=uuid.uuid4(),
        name=name,
        stage="seed",
        industry="Fintech",
        website=None,
        logo_url=None,
        company_size_range="11-50",
        founding_year=2010,
        hq_location="San Francisco, CA",
        description=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _make_company_user(company_id: uuid.UUID, email: str = "hm@acme.example.com") -> CompanyUser:
    now = datetime.now(UTC)
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id,
        supabase_user_id=None,
        email=email,
        full_name="Jane Doe",
        role="hiring_manager",
        created_at=now,
        updated_at=now,
    )


def _build_app(operator: Operator | None = None) -> FastAPI:
    """Construct a minimal FastAPI app with only the companies router.

    `get_current_operator` resolves to the supplied operator (or a fresh
    active one); `get_async_session` resolves to a bare `AsyncMock` since
    the repository methods are patched at the module boundary.
    """
    op = operator or _make_operator()

    app = FastAPI()
    app.include_router(companies_module.router, prefix="/companies")

    async def _override_operator() -> Operator:
        return op

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_current_operator] = _override_operator
    app.dependency_overrides[get_async_session] = _override_session
    return app


def _client(operator: Operator | None = None) -> TestClient:
    return TestClient(_build_app(operator))


# ---------------------------------------------------------------------------
# B1 — POST /companies
# ---------------------------------------------------------------------------


def test_create_company_success() -> None:
    op = _make_operator()
    payload: dict[str, Any] = {
        "name": "Stripe (test)",
        "stage": "growth",
        "industry": "Fintech",
        "company_size_range": "1001+",
        "founding_year": 2010,
        "hq_location": "San Francisco, CA",
    }
    created = _make_company(name=payload["name"])
    created.stage = "growth"
    created.company_size_range = "1001+"

    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_name_ci",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            companies_module.CompanyRepository,
            "create",
            new=AsyncMock(return_value=created),
        ) as mock_create,
    ):
        with _client(op) as client:
            resp = client.post("/companies", json=payload)

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["status"] is True
    assert body["data"]["id"] == str(created.id)
    assert body["data"]["name"] == payload["name"]
    assert body["data"]["stage"] == "growth"
    assert body["data"]["status"] == "active"
    mock_create.assert_awaited_once()


def test_create_company_duplicate_name_returns_409() -> None:
    payload = {"name": "Stripe (test)"}
    existing = _make_company(name="stripe (test)")  # case-insensitive match

    with patch.object(
        companies_module.CompanyRepository,
        "get_by_name_ci",
        new=AsyncMock(return_value=existing),
    ):
        with _client() as client:
            resp = client.post("/companies", json=payload)

    assert resp.status_code == status.HTTP_409_CONFLICT
    # Generic detail — must not reflect the submitted name.
    assert resp.json()["detail"] == "Company with this name already exists"


def test_create_company_unauthorized_returns_403() -> None:
    """When `get_current_operator` raises 403 the endpoint surfaces it."""
    app = FastAPI()
    app.include_router(companies_module.router, prefix="/companies")

    async def _raise_forbidden() -> Operator:  # noqa: RUF029
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required",
        )

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_current_operator] = _raise_forbidden
    app.dependency_overrides[get_async_session] = _override_session

    with TestClient(app) as client:
        resp = client.post("/companies", json={"name": "Acme"})

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Operator privileges required"


@pytest.mark.parametrize(
    "bad_payload",
    [
        # Invalid stage value.
        {"name": "Acme", "stage": "ipo"},
        # Empty name.
        {"name": ""},
        # Founding year out of range.
        {"name": "Acme", "founding_year": 1500},
    ],
)
def test_create_company_invalid_payload_returns_422(bad_payload: dict[str, Any]) -> None:
    """Pydantic validation errors short-circuit before any repository call."""
    with patch.object(
        companies_module.CompanyRepository,
        "get_by_name_ci",
        new=AsyncMock(return_value=None),
    ) as mock_lookup:
        with _client() as client:
            resp = client.post("/companies", json=bad_payload)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    mock_lookup.assert_not_awaited()


# ---------------------------------------------------------------------------
# B3 — GET /companies (paginated)
# ---------------------------------------------------------------------------


def test_list_companies_returns_paginated() -> None:
    rows = [_make_company(name=f"Co {i}") for i in range(3)]

    with patch.object(
        companies_module.CompanyRepository,
        "list_paginated",
        new=AsyncMock(return_value=(rows, 7)),
    ) as mock_list:
        with _client() as client:
            resp = client.get("/companies", params={"limit": 3, "offset": 0})

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["data"]["limit"] == 3
    assert body["data"]["offset"] == 0
    assert body["data"]["total"] == 7
    assert len(body["data"]["data"]) == 3
    assert {row["name"] for row in body["data"]["data"]} == {"Co 0", "Co 1", "Co 2"}
    mock_list.assert_awaited_once_with(limit=3, offset=0)


def test_list_companies_invalid_limit_returns_422() -> None:
    with _client() as client:
        resp = client.get("/companies", params={"limit": 0})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# B3 — GET /companies/{id}
# ---------------------------------------------------------------------------


def test_get_company_not_found_returns_404() -> None:
    with patch.object(
        companies_module.CompanyRepository,
        "get_with_users",
        new=AsyncMock(return_value=None),
    ):
        with _client() as client:
            resp = client.get(f"/companies/{uuid.uuid4()}")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Company not found"


def test_get_company_success_includes_users() -> None:
    company = _make_company(name="Acme")
    user = _make_company_user(company_id=company.id, email="hm@acme.example.com")
    # `get_with_users` would normally hydrate `.users` via selectinload.
    # Setting the attribute directly is sufficient because the response
    # projection only iterates the relationship list.
    company.users = [user]  # type: ignore[assignment]

    with patch.object(
        companies_module.CompanyRepository,
        "get_with_users",
        new=AsyncMock(return_value=company),
    ):
        with _client() as client:
            resp = client.get(f"/companies/{company.id}")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["data"]["id"] == str(company.id)
    assert body["data"]["name"] == "Acme"
    assert len(body["data"]["users"]) == 1
    assert body["data"]["users"][0]["email"] == "hm@acme.example.com"
    assert body["data"]["users"][0]["role"] == "hiring_manager"


def test_get_company_invalid_uuid_returns_422() -> None:
    with _client() as client:
        resp = client.get("/companies/not-a-uuid")
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# Silence "unused" warnings for helpers conditionally referenced in patches.
_ = MagicMock
