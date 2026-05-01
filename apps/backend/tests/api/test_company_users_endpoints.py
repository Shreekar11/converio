"""Hermetic endpoint tests for the operator-only company-user (seat) API.

Covers `POST /companies/{id}/users` and `GET /companies/{id}/users` per
phase B5 of `docs/plans/job_intake_plan.md`. Repositories are patched so
the tests run without a live PG.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import companies as companies_module
from app.core.auth import get_current_operator
from app.core.database import get_async_session
from app.database.models import Company, CompanyUser, Operator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    return Operator(
        id=uuid.uuid4(),
        supabase_user_id=f"sup-{uuid.uuid4().hex[:8]}",
        email="op@converio.test",
        full_name="Test Operator",
        status="active",
    )


def _make_company() -> Company:
    now = datetime.now(UTC)
    return Company(
        id=uuid.uuid4(),
        name="Acme",
        stage="seed",
        status="active",
        created_at=now,
        updated_at=now,
    )


def _make_company_user(
    company_id: uuid.UUID, email: str = "hm@acme.example.com", role: str = "hiring_manager"
) -> CompanyUser:
    now = datetime.now(UTC)
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id,
        supabase_user_id=None,
        email=email,
        full_name="Jane Doe",
        role=role,
        created_at=now,
        updated_at=now,
    )


def _client(operator: Operator | None = None) -> TestClient:
    op = operator or _make_operator()
    app = FastAPI()
    app.include_router(companies_module.router, prefix="/companies")

    async def _override_operator() -> Operator:
        return op

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_current_operator] = _override_operator
    app.dependency_overrides[get_async_session] = _override_session
    return TestClient(app)


# ---------------------------------------------------------------------------
# B2 — POST /companies/{company_id}/users
# ---------------------------------------------------------------------------


def test_provision_user_success() -> None:
    company = _make_company()
    new_user = _make_company_user(company_id=company.id, email="hm@acme.example.com")

    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "get_by_email",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "create",
            new=AsyncMock(return_value=new_user),
        ) as mock_create,
    ):
        with _client() as client:
            resp = client.post(
                f"/companies/{company.id}/users",
                json={
                    "email": "hm@acme.example.com",
                    "full_name": "Jane Doe",
                    "role": "hiring_manager",
                },
            )

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["data"]["email"] == "hm@acme.example.com"
    assert body["data"]["role"] == "hiring_manager"
    assert body["data"]["company_id"] == str(company.id)
    mock_create.assert_awaited_once()


def test_provision_user_duplicate_email_returns_409() -> None:
    company = _make_company()
    existing = _make_company_user(company_id=company.id, email="hm@acme.example.com")

    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "get_by_email",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "create",
            new=AsyncMock(),
        ) as mock_create,
    ):
        with _client() as client:
            resp = client.post(
                f"/companies/{company.id}/users",
                json={"email": "hm@acme.example.com", "role": "hiring_manager"},
            )

    assert resp.status_code == status.HTTP_409_CONFLICT
    assert resp.json()["detail"] == "User with this email already exists"
    mock_create.assert_not_awaited()


def test_provision_user_company_not_found_returns_404() -> None:
    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "get_by_email",
            new=AsyncMock(),
        ) as mock_email_lookup,
    ):
        with _client() as client:
            resp = client.post(
                f"/companies/{uuid.uuid4()}/users",
                json={"email": "hm@acme.example.com", "role": "hiring_manager"},
            )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Company not found"
    # We must short-circuit before hitting the duplicate-email check.
    mock_email_lookup.assert_not_awaited()


def test_provision_user_invalid_role_returns_422() -> None:
    company = _make_company()
    with patch.object(
        companies_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ):
        with _client() as client:
            resp = client.post(
                f"/companies/{company.id}/users",
                json={"email": "hm@acme.example.com", "role": "ceo"},
            )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# B3 — GET /companies/{company_id}/users
# ---------------------------------------------------------------------------


def test_list_company_users_returns_seats() -> None:
    company = _make_company()
    users = [
        _make_company_user(company_id=company.id, email="a@acme.example.com"),
        _make_company_user(
            company_id=company.id, email="admin@acme.example.com", role="admin"
        ),
    ]

    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "list_for_company",
            new=AsyncMock(return_value=users),
        ),
    ):
        with _client() as client:
            resp = client.get(f"/companies/{company.id}/users")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    emails = {row["email"] for row in body["data"]["data"]}
    assert emails == {"a@acme.example.com", "admin@acme.example.com"}


def test_list_company_users_company_not_found_returns_404() -> None:
    with (
        patch.object(
            companies_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            companies_module.CompanyUserRepository,
            "list_for_company",
            new=AsyncMock(),
        ) as mock_list,
    ):
        with _client() as client:
            resp = client.get(f"/companies/{uuid.uuid4()}/users")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Company not found"
    mock_list.assert_not_awaited()
