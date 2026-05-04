"""Hermetic endpoint tests for `POST /api/v1/jobs/intake` (Phase E1, T4.2).

Strategy (per `.claude/rules/test-driven-development.mdc`):
- The DB session, repositories, Temporal client, and rate limiter are all
  mocked. No PG, no Temporal, no Supabase — these tests run in-process.
- We mount only the `jobs` router under the `/jobs` prefix on a minimal
  FastAPI app. `get_current_active_company_user` and `get_async_session`
  are overridden via FastAPI's dep-override mechanism. The endpoint no
  longer resolves operator vs company_user inline; the auth dep is the
  single source of truth for identity + active-company gating.

T4.2 gates this endpoint on `Company.status == "active"`. The dep
`get_current_active_company_user` raises 403 with `detail="Company not
active"` for `pending_review` / `paused` / `churned` companies. We
exercise that path by overriding the dep to raise (which is what the
real dep does on a non-active company in production).

Coverage maps directly to the Phase E plan test cases (see
`docs/plans/job_intake_plan.md` E1) plus the T4.2 active-status gate.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDReusePolicy

from app.api.v1.endpoints import jobs as jobs_module
from app.core.auth import get_current_active_company_user
from app.core.database import get_async_session
from app.core.rate_limit import job_intake_rate_limiter
from app.database.models import Company, CompanyUser


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_company(
    company_id: uuid.UUID | None = None, *, status_value: str = "active"
) -> Company:
    return Company(
        id=company_id or uuid.uuid4(),
        name="Acme",
        stage="seed",
        status=status_value,
    )


def _make_company_user(
    company_id: uuid.UUID,
    supabase_user_id: str | None = None,
    email: str = "hm@acme.example.com",
) -> CompanyUser:
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id,
        supabase_user_id=supabase_user_id or f"sup-{uuid.uuid4().hex[:8]}",
        email=email,
        full_name="Hiring Manager",
        role="hiring_manager",
    )


def _build_app(
    company_user: CompanyUser | None = None,
    *,
    forbid_detail: str | None = None,
) -> FastAPI:
    """Mount only the jobs router and override the active-company-user dep.

    If `forbid_detail` is set, the dep is overridden to raise 403 with that
    detail — simulating a non-active company (T4.2). Otherwise it returns
    `company_user` directly, simulating a successful auth check.
    """
    app = FastAPI()
    app.include_router(jobs_module.router, prefix="/jobs")

    if forbid_detail is not None:
        async def _override_active_company_user() -> CompanyUser:  # noqa: RUF029
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=forbid_detail,
            )
    else:
        assert company_user is not None, (
            "either company_user or forbid_detail must be provided"
        )

        async def _override_active_company_user() -> CompanyUser:
            return company_user

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_current_active_company_user] = (
        _override_active_company_user
    )
    app.dependency_overrides[get_async_session] = _override_session
    return app


def _client(company_user: CompanyUser) -> TestClient:
    return TestClient(_build_app(company_user))


def _valid_payload(company_id: uuid.UUID) -> dict[str, Any]:
    return {
        "company_id": str(company_id),
        "title": "Founding Engineer",
        "jd_text": "Build the backbone of our platform — distributed systems experience required.",
        "intake_notes": "small team, generalist preferred",
        "remote_onsite": "hybrid",
        "location_text": "SF Bay Area",
        "compensation_min": 180000,
        "compensation_max": 240000,
    }


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts from an empty rate-limiter bucket map."""
    job_intake_rate_limiter.reset()
    yield
    job_intake_rate_limiter.reset()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_intake_as_active_company_user_success() -> None:
    """Active company user submits intake; company_id sourced from auth context."""
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    with (
        patch.object(
            jobs_module.JobRepository,
            "create",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_job_create,
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    body = resp.json()
    assert body["status"] is True
    data = body["data"]
    job_id = data["job_id"]
    workflow_id = data["workflow_id"]
    assert workflow_id == f"job-intake-{job_id}"
    assert data["status"] == "intake"

    # Job row inserted with company_id from auth context, created_by from
    # the authenticated company_user.
    assert mock_job_create.await_count == 1
    create_kwargs = mock_job_create.await_args.kwargs
    assert create_kwargs["created_by"] == company_user.id
    assert create_kwargs["company_id"] == company.id
    assert str(create_kwargs["id"]) == job_id
    assert create_kwargs["status"] == "intake"
    assert create_kwargs["workflow_id"] == workflow_id

    # Workflow started fire-and-forget with the right knobs.
    assert fake_temporal_client.start_workflow.await_count == 1
    sw_args, sw_kwargs = fake_temporal_client.start_workflow.await_args
    assert sw_args[0] == "JobIntakeWorkflow"
    assert sw_kwargs["id"] == workflow_id
    assert sw_kwargs["task_queue"] == "converio-queue"
    assert sw_kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    wf_input = sw_args[1]
    assert wf_input["job_id"] == job_id
    assert wf_input["title"] == payload["title"]


def test_submit_intake_ignores_company_id_in_request_body() -> None:
    """Tenant scoping: company_id from auth wins; body's company_id is ignored.

    A seated user at company A who tries to intake against company B (by
    setting body.company_id=B) still gets the job written under company A.
    Prevents cross-tenant escalation via request-body forgery.
    """
    seated_company = _make_company(status_value="active")
    other_company_id = uuid.uuid4()
    company_user = _make_company_user(seated_company.id)

    payload = _valid_payload(other_company_id)  # body claims a different company

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    with (
        patch.object(
            jobs_module.JobRepository,
            "create",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_job_create,
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    create_kwargs = mock_job_create.await_args.kwargs
    # Auth context wins: row written under the seated company, NOT the body claim.
    assert create_kwargs["company_id"] == seated_company.id
    assert create_kwargs["company_id"] != other_company_id


# ---------------------------------------------------------------------------
# T4.2 — Active company status gate
# ---------------------------------------------------------------------------


def test_submit_intake_pending_review_company_returns_403() -> None:
    """Company in `pending_review` status -> 403 'Company not active'."""
    payload = _valid_payload(uuid.uuid4())

    job_create = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock()

    app = _build_app(forbid_detail="Company not active")

    with (
        patch.object(jobs_module.JobRepository, "create", new=job_create),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    detail = resp.json()["detail"]
    assert "not active" in detail.lower()
    # 403 short-circuits before any DB write or workflow start.
    job_create.assert_not_awaited()
    fake_temporal_client.start_workflow.assert_not_awaited()


def test_submit_intake_paused_company_returns_403() -> None:
    """Company in `paused` status -> 403 'Company not active'."""
    payload = _valid_payload(uuid.uuid4())

    job_create = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock()

    app = _build_app(forbid_detail="Company not active")

    with (
        patch.object(jobs_module.JobRepository, "create", new=job_create),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    detail = resp.json()["detail"]
    assert "not active" in detail.lower()
    job_create.assert_not_awaited()
    fake_temporal_client.start_workflow.assert_not_awaited()


def test_submit_intake_churned_company_returns_403() -> None:
    """Company in `churned` status -> 403 'Company not active'."""
    payload = _valid_payload(uuid.uuid4())

    job_create = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock()

    app = _build_app(forbid_detail="Company not active")

    with (
        patch.object(jobs_module.JobRepository, "create", new=job_create),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    detail = resp.json()["detail"]
    assert "not active" in detail.lower()
    job_create.assert_not_awaited()
    fake_temporal_client.start_workflow.assert_not_awaited()


def test_submit_intake_no_company_user_seat_returns_403() -> None:
    """Authenticated Supabase user with no CompanyUser row -> 403."""
    payload = _valid_payload(uuid.uuid4())

    job_create = AsyncMock()
    app = _build_app(forbid_detail="Not a company user")

    with patch.object(jobs_module.JobRepository, "create", new=job_create):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not a company user"
    job_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_submit_intake_invalid_body_missing_jd_text() -> None:
    """Pydantic-level validation short-circuits before any IO."""
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    bad_payload: dict[str, Any] = {
        "company_id": str(company.id),
        "title": "Founding Engineer",
        # jd_text is required.
    }

    with patch.object(
        jobs_module.JobRepository, "create", new=AsyncMock()
    ) as mock_create:
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=bad_payload)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    mock_create.assert_not_awaited()


def test_submit_intake_compensation_min_gt_max() -> None:
    """compensation_min > compensation_max is enforced at the HTTP boundary.

    The generated `JobIntakeRequest` schema does not encode this cross-field
    constraint (so this is NOT caught by FastAPI's default body validation),
    but the endpoint constructs `JobIntakeInput` BEFORE inserting the Job row
    and surfaces the resulting `ValidationError` as a 422. This test asserts
    that contract — and that no DB write or workflow start happens on a 422.
    """
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    payload = _valid_payload(company.id)
    payload["compensation_min"] = 300000
    payload["compensation_max"] = 100000

    job_create = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock()

    with (
        patch.object(jobs_module.JobRepository, "create", new=job_create),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    job_create.assert_not_awaited()
    fake_temporal_client.start_workflow.assert_not_awaited()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_submit_intake_rate_limit_exceeded() -> None:
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    # Sanity check the limiter bucket math directly, then reset.
    company_key = str(company.id)
    for _ in range(job_intake_rate_limiter.max_requests):
        assert job_intake_rate_limiter.check(company_key) is True
    assert job_intake_rate_limiter.check(company_key) is False
    job_intake_rate_limiter.reset()

    with (
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            for _ in range(job_intake_rate_limiter.max_requests):
                ok = client.post("/jobs/intake", json=payload)
                assert ok.status_code == status.HTTP_202_ACCEPTED, ok.text

            blocked = client.post("/jobs/intake", json=payload)

    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert blocked.json()["detail"] == "Job intake rate limit exceeded for this company"
    assert blocked.headers.get("Retry-After") == str(
        job_intake_rate_limiter.window_seconds
    )


# ---------------------------------------------------------------------------
# Temporal failures
# ---------------------------------------------------------------------------


def test_submit_intake_temporal_failure_returns_500() -> None:
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(
        side_effect=RuntimeError(
            "temporal frontend unreachable: dial tcp 127.0.0.1:7233"
        )
    )

    with (
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    body = resp.json()
    # Generic detail — no echo of the underlying exception text.
    assert body["detail"] == "Failed to start job intake workflow"
    assert "temporal frontend unreachable" not in body["detail"]


# ---------------------------------------------------------------------------
# PII / log hygiene
# ---------------------------------------------------------------------------


def test_submit_intake_does_not_log_jd_text(caplog: pytest.LogCaptureFixture) -> None:
    company = _make_company(status_value="active")
    company_user = _make_company_user(company.id)
    payload = _valid_payload(company.id)
    # Sentinel substring that would only appear in logs if the endpoint
    # echoed jd_text or intake_notes.
    sentinel_jd = "SENTINEL-JD-CONTENT-NEVER-IN-LOGS-7c8f"
    sentinel_notes = "SENTINEL-NOTES-NEVER-IN-LOGS-3a91"
    payload["jd_text"] = (
        f"Build distributed systems. {sentinel_jd}. Looking for senior generalist."
    )
    payload["intake_notes"] = f"call notes: {sentinel_notes}"

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    caplog.set_level(logging.DEBUG, logger="app.api.v1.endpoints.jobs")

    with (
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with _client(company_user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text

    # No log record (message OR structured field via record.__dict__) may
    # contain the JD body or intake notes.
    for record in caplog.records:
        rendered = record.getMessage()
        assert sentinel_jd not in rendered, f"jd_text leaked into log: {rendered}"
        assert sentinel_notes not in rendered, f"intake_notes leaked into log: {rendered}"
        for value in record.__dict__.values():
            if isinstance(value, str):
                assert sentinel_jd not in value
                assert sentinel_notes not in value
