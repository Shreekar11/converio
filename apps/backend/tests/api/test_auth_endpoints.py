"""Hermetic endpoint tests for the self-serve auth router.

The auth endpoints expose three handlers — only `GET /auth/me` is
implemented at this point in the plan; the two signup handlers remain
stubbed (501). These tests cover:

- Router wiring (mounted under `/auth`, `/me` exists).
- `get_current_user` dependency rejects unauthenticated calls (401).
- `GET /auth/me` lookup precedence: operator -> company_user -> recruiter
  -> email backfill -> unregistered.

Strategy mirrors `tests/api/test_companies_endpoints.py`: build a minimal
FastAPI app with only the auth router mounted, override `get_current_user`
and `get_async_session`, and patch repository methods at the module
boundary so no DB IO occurs.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import auth as auth_module
from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.rate_limit import signup_rate_limiter
from app.database.models import Company, CompanyUser, Operator, Recruiter


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_current_user(
    sub: str | None = None, email: str | None = "user@example.test"
) -> CurrentUser:
    return CurrentUser(
        id=sub or f"sup-{uuid.uuid4().hex[:8]}",
        email=email,
        role="user",
        app_metadata={},
        user_metadata={},
    )


def _make_operator(sub: str) -> Operator:
    now = datetime.now(UTC)
    return Operator(
        id=uuid.uuid4(),
        supabase_user_id=sub,
        email="op@converio.test",
        full_name="Test Operator",
        status="active",
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
        website=None,
        logo_url=None,
        company_size_range="11-50",
        founding_year=2010,
        hq_location="San Francisco, CA",
        description=None,
        status=status_value,
        created_at=now,
        updated_at=now,
    )


def _make_company_user(
    company_id: uuid.UUID,
    sub: str | None,
    email: str = "hm@acme.example.com",
) -> CompanyUser:
    now = datetime.now(UTC)
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id,
        supabase_user_id=sub,
        email=email,
        full_name="Jane Doe",
        role="hiring_manager",
        created_at=now,
        updated_at=now,
    )


def _make_recruiter(sub: str, status_value: str = "active") -> Recruiter:
    now = datetime.now(UTC)
    return Recruiter(
        id=uuid.uuid4(),
        supabase_user_id=sub,
        full_name="Pat Recruiter",
        email="recruiter@example.test",
        linkedin_url=None,
        bio=None,
        recruited_funding_stage="seed",
        workspace_type="agency",
        domain_expertise=["fintech", "ai_infra"],
        acceptance_rate=None,
        avg_days_to_close=None,
        fill_rate_pct=None,
        total_placements=3,
        at_capacity=False,
        status=status_value,
        embedding=None,
        extra=None,
        created_at=now,
        updated_at=now,
    )


def _build_app(current_user: CurrentUser | None = None) -> FastAPI:
    """Construct a minimal FastAPI app with only the auth router mounted.

    `get_current_user` is overridden to return the supplied user; if `None`
    is passed the override is *not* installed, so the dependency runs and
    returns 401 for un-authed callers.
    """
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/auth")

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_async_session] = _override_session

    if current_user is not None:
        async def _override_user() -> CurrentUser:
            return current_user

        app.dependency_overrides[get_current_user] = _override_user

    return app


def _client(current_user: CurrentUser | None = None) -> TestClient:
    return TestClient(_build_app(current_user))


# ---------------------------------------------------------------------------
# Router wiring / unauthenticated access (T2.2 smoke — kept here)
# ---------------------------------------------------------------------------


def test_router_registered_requires_auth() -> None:
    """`GET /auth/me` without a Bearer token must return 401, not 501.

    Proves the router is wired up (any other path would 404) and that
    `get_current_user` runs before the handler — unauthenticated callers
    are rejected at the dependency layer.
    """
    with TestClient(_build_app()) as client:
        resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text
    assert resp.json()["detail"] == "Authorization header missing"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# T3.1 — GET /auth/me identity resolver
# ---------------------------------------------------------------------------


def test_auth_me_returns_operator_role() -> None:
    """Operator row found -> role=operator, profile shape matches helper."""
    user = _make_current_user()
    op = _make_operator(user.id)

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=op),
    ) as mock_op_lookup, patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ) as mock_cu_lookup:
        with _client(user) as client:
            resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["status"] is True
    assert body["data"]["role"] == "operator"
    assert body["data"]["profile"]["id"] == str(op.id)
    assert body["data"]["profile"]["email"] == op.email
    assert body["data"]["profile"]["status"] == "active"
    assert body["data"]["onboarding_state"] is None
    mock_op_lookup.assert_awaited_once_with(user.id)
    # Operator hit short-circuits — company_user lookup must not run.
    mock_cu_lookup.assert_not_awaited()


def test_auth_me_returns_company_user_role() -> None:
    """CompanyUser linked + active company -> role=company_user with onboarding_state."""
    user = _make_current_user()
    company = _make_company(status_value="active")
    cu = _make_company_user(company.id, sub=user.id)

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=cu),
    ), patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ) as mock_company_lookup, patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ) as mock_rec_lookup:
        with _client(user) as client:
            resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["data"]["role"] == "company_user"
    assert body["data"]["profile"]["user"]["id"] == str(cu.id)
    assert body["data"]["profile"]["user"]["email"] == cu.email
    assert body["data"]["profile"]["company"]["id"] == str(company.id)
    assert body["data"]["profile"]["company"]["status"] == "active"
    assert body["data"]["onboarding_state"] == {"company_status": "active"}
    mock_company_lookup.assert_awaited_once_with(cu.company_id)
    mock_rec_lookup.assert_not_awaited()


def test_auth_me_returns_recruiter_role() -> None:
    """Recruiter linked -> role=recruiter with recruiter_status onboarding_state."""
    user = _make_current_user()
    rec = _make_recruiter(user.id, status_value="pending")

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=rec),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ) as mock_email_lookup:
        with _client(user) as client:
            resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["data"]["role"] == "recruiter"
    assert body["data"]["profile"]["id"] == str(rec.id)
    assert body["data"]["profile"]["status"] == "pending"
    assert body["data"]["profile"]["domain_expertise"] == ["fintech", "ai_infra"]
    assert body["data"]["profile"]["at_capacity"] is False
    assert body["data"]["profile"]["total_placements"] == 3
    assert body["data"]["onboarding_state"] == {"recruiter_status": "pending"}
    # Recruiter hit short-circuits — email backfill must not run.
    mock_email_lookup.assert_not_awaited()


def test_auth_me_returns_unregistered() -> None:
    """All lookups miss -> role=unregistered, profile=null, onboarding=null."""
    user = _make_current_user()

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "link_supabase_user_id",
        new=AsyncMock(),
    ) as mock_link:
        with _client(user) as client:
            resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["data"]["role"] == "unregistered"
    assert body["data"]["profile"] is None
    assert body["data"]["onboarding_state"] is None
    mock_link.assert_not_awaited()


def test_auth_me_backfills_provisioned_seat() -> None:
    """Pre-provisioned seat (supabase_user_id=null) gets linked on first sign-in.

    All `supabase_user_id` lookups miss; `get_by_email` returns the unlinked
    seat; `link_supabase_user_id` is invoked and its return value drives the
    company_user response.
    """
    email = "preseated@acme.example.com"
    user = _make_current_user(email=email)
    company = _make_company(status_value="pending_review")
    unlinked = _make_company_user(company.id, sub=None, email=email)
    linked = _make_company_user(company.id, sub=user.id, email=email)
    # The link helper returns the freshly-linked row — keep id stable so the
    # test can assert on the same id pre/post link.
    linked.id = unlinked.id

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=unlinked),
    ) as mock_email_lookup, patch.object(
        auth_module.CompanyUserRepository,
        "link_supabase_user_id",
        new=AsyncMock(return_value=linked),
    ) as mock_link, patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ) as mock_company_lookup:
        with _client(user) as client:
            resp = client.get("/auth/me")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["data"]["role"] == "company_user"
    assert body["data"]["profile"]["user"]["id"] == str(linked.id)
    assert body["data"]["profile"]["company"]["id"] == str(company.id)
    assert body["data"]["onboarding_state"] == {
        "company_status": "pending_review"
    }
    mock_email_lookup.assert_awaited_once_with(email)
    mock_link.assert_awaited_once_with(unlinked.id, user.id)
    mock_company_lookup.assert_awaited_once_with(linked.company_id)


# ---------------------------------------------------------------------------
# T3.2 — POST /auth/company/signup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_signup_rate_limiter():
    """Each test starts with an empty signup rate-limiter bucket map."""
    signup_rate_limiter.reset()
    yield
    signup_rate_limiter.reset()


def _signup_payload(name: str = "Acme Inc") -> dict:
    return {
        "name": name,
        "stage": "seed",
        "industry": "Fintech",
        "website": "https://acme.example.com",
        "logo_url": "https://acme.example.com/logo.png",
        "company_size_range": "11-50",
        "founding_year": 2020,
        "hq_location": "San Francisco, CA",
        "description": "Test company",
    }


def _signup_user(email: str = "founder@acme.example.com") -> CurrentUser:
    return CurrentUser(
        id=f"sup-{uuid.uuid4().hex[:8]}",
        email=email,
        role="user",
        app_metadata={},
        user_metadata={"full_name": "Founder Person"},
    )


def test_company_signup_success_creates_company_and_seat() -> None:
    """Happy path — JWT user signs up a brand-new company; status=pending_review."""
    user = _signup_user()
    payload = _signup_payload(name="Acme Inc")
    company = _make_company(status_value="pending_review")
    company.name = payload["name"]
    company_user = _make_company_user(
        company.id, sub=user.id, email=user.email
    )

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyRepository,
        "get_by_name_ci",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyRepository,
        "create",
        new=AsyncMock(return_value=company),
    ) as mock_company_create, patch.object(
        auth_module.CompanyUserRepository,
        "create",
        new=AsyncMock(return_value=company_user),
    ) as mock_user_create:
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["status"] is True
    assert body["data"]["user"]["id"] == str(company_user.id)
    assert body["data"]["company"]["id"] == str(company.id)
    assert body["data"]["company"]["status"] == "pending_review"

    # Company create called with status=pending_review and AnyUrl coerced to str
    create_kwargs = mock_company_create.await_args.kwargs
    assert create_kwargs["status"] == "pending_review"
    assert create_kwargs["website"] == "https://acme.example.com/"
    assert create_kwargs["logo_url"] == "https://acme.example.com/logo.png"

    # CompanyUser create binds supabase_user_id from JWT, role=admin, email from JWT
    user_kwargs = mock_user_create.await_args.kwargs
    assert user_kwargs["supabase_user_id"] == user.id
    assert user_kwargs["role"] == "admin"
    assert user_kwargs["email"] == user.email
    assert user_kwargs["full_name"] == "Founder Person"
    assert user_kwargs["company_id"] == company.id


def test_company_signup_links_preprovisioned_seat() -> None:
    """Operator pre-seated this email -> link path; no new company is created."""
    email = "preseated@acme.example.com"
    user = _signup_user(email=email)
    payload = _signup_payload()
    company = _make_company(status_value="pending_review")
    unlinked = _make_company_user(company.id, sub=None, email=email)
    linked = _make_company_user(company.id, sub=user.id, email=email)
    linked.id = unlinked.id

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=unlinked),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "link_supabase_user_id",
        new=AsyncMock(return_value=linked),
    ) as mock_link, patch.object(
        auth_module.CompanyRepository,
        "get_by_id",
        new=AsyncMock(return_value=company),
    ), patch.object(
        auth_module.CompanyRepository,
        "create",
        new=AsyncMock(),
    ) as mock_company_create, patch.object(
        auth_module.CompanyUserRepository,
        "create",
        new=AsyncMock(),
    ) as mock_user_create:
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["data"]["role"] == "company_user"
    assert body["data"]["profile"]["user"]["id"] == str(linked.id)
    assert body["data"]["profile"]["company"]["id"] == str(company.id)
    assert body["data"]["onboarding_state"] == {
        "company_status": "pending_review"
    }
    mock_link.assert_awaited_once_with(unlinked.id, user.id)
    # Standard signup path must NOT execute when seat is linked
    mock_company_create.assert_not_awaited()
    mock_user_create.assert_not_awaited()


def test_company_signup_rate_limited_returns_429() -> None:
    """Rate limiter check returning False -> 429 with Retry-After header."""
    user = _signup_user()
    payload = _signup_payload()

    with patch.object(
        auth_module.signup_rate_limiter, "check", return_value=False
    ):
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS, resp.text
    assert resp.headers.get("Retry-After") is not None


def test_company_signup_email_in_use_by_operator_returns_409() -> None:
    """OperatorRepo.get_by_email returns Operator -> 409 email_in_use_operator."""
    user = _signup_user()
    payload = _signup_payload()
    operator = _make_operator("sup-other")

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=operator),
    ):
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Email already in use"
    assert resp.headers.get("X-Error-Code") == "email_in_use_operator"


def test_company_signup_email_in_use_by_recruiter_returns_409() -> None:
    """RecruiterRepo.get_by_email returns Recruiter -> 409 email_in_use_recruiter."""
    user = _signup_user()
    payload = _signup_payload()
    recruiter = _make_recruiter("sup-other")

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_email",
        new=AsyncMock(return_value=recruiter),
    ):
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Email already in use"
    assert resp.headers.get("X-Error-Code") == "email_in_use_recruiter"


def test_company_signup_already_onboarded_returns_409() -> None:
    """CompanyUserRepo.get_by_supabase_user_id returns row -> 409 already_onboarded."""
    user = _signup_user()
    payload = _signup_payload()
    existing_company = _make_company(status_value="active")
    existing = _make_company_user(existing_company.id, sub=user.id)

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=existing),
    ):
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Already onboarded as company user"
    assert resp.headers.get("X-Error-Code") == "already_onboarded"


def test_company_signup_duplicate_company_name_returns_409() -> None:
    """get_by_name_ci returns row -> 409 'Company name already exists'."""
    user = _signup_user()
    payload = _signup_payload(name="Acme Inc")
    duplicate = _make_company(status_value="active")
    duplicate.name = "Acme Inc"

    with patch.object(
        auth_module.OperatorRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.RecruiterRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_supabase_user_id",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyUserRepository,
        "get_by_email",
        new=AsyncMock(return_value=None),
    ), patch.object(
        auth_module.CompanyRepository,
        "get_by_name_ci",
        new=AsyncMock(return_value=duplicate),
    ), patch.object(
        auth_module.CompanyRepository,
        "create",
        new=AsyncMock(),
    ) as mock_company_create:
        with _client(user) as client:
            resp = client.post("/auth/company/signup", json=payload)

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Company name already exists"
    mock_company_create.assert_not_awaited()
