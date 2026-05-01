"""Hermetic endpoint tests for `POST /api/v1/jobs/intake` (Phase E1).

Strategy (per `.claude/rules/test-driven-development.mdc`):
- The DB session, repositories, Temporal client, and rate limiter are all
  mocked. No PG, no Temporal, no Supabase — these tests run in-process.
- We mount only the `jobs` router under the `/jobs` prefix on a minimal
  FastAPI app. `get_current_user` and `get_async_session` are overridden
  via FastAPI's dep-override mechanism; the endpoint's actor-resolution
  path (operator vs company_user) is exercised by patching the repository
  and `select(...)`-based lookup at the module boundary.

Coverage maps directly to the Phase E plan test cases (see
`docs/plans/job_intake_plan.md` E1 + the agent dispatch brief).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDReusePolicy

from app.api.v1.endpoints import jobs as jobs_module
from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.rate_limit import job_intake_rate_limiter
from app.database.models import Company, CompanyUser, Operator


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_current_user(user_id: str | None = None) -> CurrentUser:
    return CurrentUser(
        id=user_id or f"sup-{uuid.uuid4().hex[:8]}",
        email="user@example.test",
        role="user",
    )


def _make_operator(supabase_user_id: str, *, status_value: str = "active") -> Operator:
    return Operator(
        id=uuid.uuid4(),
        supabase_user_id=supabase_user_id,
        email="op@converio.test",
        full_name="Test Operator",
        status=status_value,
    )


def _make_company(company_id: uuid.UUID | None = None) -> Company:
    return Company(
        id=company_id or uuid.uuid4(),
        name="Acme",
        stage="seed",
        status="active",
    )


def _make_company_user(
    company_id: uuid.UUID, supabase_user_id: str, email: str = "hm@acme.example.com"
) -> CompanyUser:
    return CompanyUser(
        id=uuid.uuid4(),
        company_id=company_id,
        supabase_user_id=supabase_user_id,
        email=email,
        full_name="Hiring Manager",
        role="hiring_manager",
    )


def _build_app(current_user: CurrentUser) -> FastAPI:
    """Mount only the jobs router and override JWT + session deps."""
    app = FastAPI()
    app.include_router(jobs_module.router, prefix="/jobs")

    async def _override_user() -> CurrentUser:
        return current_user

    async def _override_session():
        # `AsyncMock` covers `await session.execute(...)` invoked inside
        # `_resolve_actor` for the `CompanyUser` lookup. Per-test patches
        # tune `.execute(...).scalar_one_or_none()` as needed.
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_async_session] = _override_session
    return app


def _client(current_user: CurrentUser | None = None) -> TestClient:
    return TestClient(_build_app(current_user or _make_current_user()))


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


def _stub_company_user_lookup(session_mock: AsyncMock, value: CompanyUser | None) -> None:
    """Wire `session.execute(...).scalar_one_or_none()` to return `value`."""
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = value
    session_mock.execute = AsyncMock(return_value=exec_result)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts from an empty rate-limiter bucket map."""
    job_intake_rate_limiter.reset()
    yield
    job_intake_rate_limiter.reset()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_submit_intake_as_operator_success() -> None:
    user = _make_current_user()
    operator = _make_operator(user.id)
    company = _make_company()
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
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
        with _client(user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    body = resp.json()
    assert body["status"] is True
    data = body["data"]
    job_id = data["job_id"]
    workflow_id = data["workflow_id"]
    assert workflow_id == f"job-intake-{job_id}"
    assert data["status"] == "intake"

    # Job row was inserted with operator path -> created_by=None.
    assert mock_job_create.await_count == 1
    create_kwargs = mock_job_create.await_args.kwargs
    assert create_kwargs["created_by"] is None
    assert str(create_kwargs["id"]) == job_id
    assert create_kwargs["company_id"] == company.id
    assert create_kwargs["status"] == "intake"
    assert create_kwargs["workflow_id"] == workflow_id

    # Workflow started fire-and-forget with the right knobs.
    assert fake_temporal_client.start_workflow.await_count == 1
    sw_args, sw_kwargs = fake_temporal_client.start_workflow.await_args
    assert sw_args[0] == "JobIntakeWorkflow"
    assert sw_kwargs["id"] == workflow_id
    assert sw_kwargs["task_queue"] == "converio-queue"
    assert sw_kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    # The payload to the workflow is a plain JSON-serializable dict.
    wf_input = sw_args[1]
    assert wf_input["job_id"] == job_id
    assert wf_input["title"] == payload["title"]


def test_submit_intake_as_company_user_success() -> None:
    user = _make_current_user()
    company = _make_company()
    company_user = _make_company_user(company.id, supabase_user_id=user.id)
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    # Override the session to return the company_user from the inline
    # `select(CompanyUser)` lookup in `_resolve_actor`.
    session_mock = AsyncMock()
    _stub_company_user_lookup(session_mock, company_user)

    async def _session_override():
        yield session_mock

    app = _build_app(user)
    app.dependency_overrides[get_async_session] = _session_override

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            jobs_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
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
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    # company_user path -> created_by is the company_user's id.
    create_kwargs = mock_job_create.await_args.kwargs
    assert create_kwargs["created_by"] == company_user.id


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------


def test_submit_intake_as_company_user_wrong_company() -> None:
    user = _make_current_user()
    target_company = _make_company()
    other_company_id = uuid.uuid4()
    seated_user = _make_company_user(other_company_id, supabase_user_id=user.id)
    payload = _valid_payload(target_company.id)

    session_mock = AsyncMock()
    _stub_company_user_lookup(session_mock, seated_user)

    async def _session_override():
        yield session_mock

    app = _build_app(user)
    app.dependency_overrides[get_async_session] = _session_override

    company_get = AsyncMock(return_value=target_company)
    job_create = AsyncMock()

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(jobs_module.CompanyRepository, "get_by_id", new=company_get),
        patch.object(jobs_module.JobRepository, "create", new=job_create),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized for this company"
    # 403 short-circuits before any DB write or company lookup.
    job_create.assert_not_awaited()
    company_get.assert_not_awaited()


def test_submit_intake_no_actor_row() -> None:
    """Neither operator nor company_user row exists -> 403, no leakage."""
    user = _make_current_user()
    company_id = uuid.uuid4()
    payload = _valid_payload(company_id)

    session_mock = AsyncMock()
    _stub_company_user_lookup(session_mock, None)

    async def _session_override():
        yield session_mock

    app = _build_app(user)
    app.dependency_overrides[get_async_session] = _session_override

    with patch.object(
        jobs_module.OperatorRepository,
        "get_by_supabase_id",
        new=AsyncMock(return_value=None),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized for this company"


def test_submit_intake_inactive_operator_falls_back_to_company_user() -> None:
    """An inactive Operator row must NOT grant operator privileges. The
    endpoint should fall through to the company_user resolution path.
    """
    user = _make_current_user()
    company = _make_company()
    inactive_operator = _make_operator(user.id, status_value="inactive")
    company_user = _make_company_user(company.id, supabase_user_id=user.id)
    payload = _valid_payload(company.id)

    session_mock = AsyncMock()
    _stub_company_user_lookup(session_mock, company_user)

    async def _session_override():
        yield session_mock

    app = _build_app(user)
    app.dependency_overrides[get_async_session] = _session_override

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=inactive_operator),
        ),
        patch.object(
            jobs_module.CompanyRepository,
            "get_by_id",
            new=AsyncMock(return_value=company),
        ),
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ) as mock_job_create,
        patch.object(
            jobs_module, "get_temporal_client", new=AsyncMock(return_value=fake_temporal_client)
        ),
    ):
        with TestClient(app) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    create_kwargs = mock_job_create.await_args.kwargs
    assert create_kwargs["created_by"] == company_user.id


# ---------------------------------------------------------------------------
# Validation / not found
# ---------------------------------------------------------------------------


def test_submit_intake_company_not_found() -> None:
    user = _make_current_user()
    operator = _make_operator(user.id)
    payload = _valid_payload(uuid.uuid4())

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock(return_value=None)
        ),
        patch.object(jobs_module.JobRepository, "create", new=AsyncMock()) as mock_create,
    ):
        with _client(user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Company not found"
    mock_create.assert_not_awaited()


def test_submit_intake_invalid_body_missing_jd_text() -> None:
    """Pydantic-level validation short-circuits before any IO."""
    user = _make_current_user()
    bad_payload: dict[str, Any] = {
        "company_id": str(uuid.uuid4()),
        "title": "Founding Engineer",
        # jd_text is required.
    }

    with (
        patch.object(
            jobs_module.OperatorRepository, "get_by_supabase_id", new=AsyncMock()
        ) as mock_op,
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock()
        ) as mock_company,
    ):
        with _client(user) as client:
            resp = client.post("/jobs/intake", json=bad_payload)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    mock_op.assert_not_awaited()
    mock_company.assert_not_awaited()


def test_submit_intake_compensation_min_gt_max() -> None:
    """compensation_min > compensation_max is enforced at the HTTP boundary.

    The generated `JobIntakeRequest` schema does not encode this cross-field
    constraint (so this is NOT caught by FastAPI's default body validation),
    but the endpoint constructs `JobIntakeInput` BEFORE inserting the Job row
    and surfaces the resulting `ValidationError` as a 422. This test asserts
    that contract — and that no DB write or workflow start happens on a 422.
    """
    user = _make_current_user()
    operator = _make_operator(user.id)
    company = _make_company()
    payload = _valid_payload(company.id)
    payload["compensation_min"] = 300000
    payload["compensation_max"] = 100000

    job_create = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock()

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock(return_value=company)
        ),
        patch.object(jobs_module.JobRepository, "create", new=job_create),
        patch.object(
            jobs_module, "get_temporal_client", new=AsyncMock(return_value=fake_temporal_client)
        ),
    ):
        with _client(user) as client:
            resp = client.post("/jobs/intake", json=payload)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    job_create.assert_not_awaited()
    fake_temporal_client.start_workflow.assert_not_awaited()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_submit_intake_rate_limit_exceeded() -> None:
    user = _make_current_user()
    operator = _make_operator(user.id)
    company = _make_company()
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(return_value=MagicMock())

    # Pre-fill the bucket up to the limit so the first request from this
    # test is the one that gets rejected.
    company_key = str(company.id)
    for _ in range(job_intake_rate_limiter.max_requests):
        assert job_intake_rate_limiter.check(company_key) is True
    assert job_intake_rate_limiter.check(company_key) is False

    # Reset and instead exhaust through the endpoint to validate the wiring.
    job_intake_rate_limiter.reset()

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock(return_value=company)
        ),
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module, "get_temporal_client", new=AsyncMock(return_value=fake_temporal_client)
        ),
    ):
        with _client(user) as client:
            for _ in range(job_intake_rate_limiter.max_requests):
                ok = client.post("/jobs/intake", json=payload)
                assert ok.status_code == status.HTTP_202_ACCEPTED, ok.text

            blocked = client.post("/jobs/intake", json=payload)

    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert blocked.json()["detail"] == "Job intake rate limit exceeded for this company"
    assert blocked.headers.get("Retry-After") == str(job_intake_rate_limiter.window_seconds)


# ---------------------------------------------------------------------------
# Temporal failures
# ---------------------------------------------------------------------------


def test_submit_intake_temporal_failure_returns_500() -> None:
    user = _make_current_user()
    operator = _make_operator(user.id)
    company = _make_company()
    payload = _valid_payload(company.id)

    fake_temporal_client = MagicMock()
    fake_temporal_client.start_workflow = AsyncMock(
        side_effect=RuntimeError("temporal frontend unreachable: dial tcp 127.0.0.1:7233")
    )

    with (
        patch.object(
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock(return_value=company)
        ),
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module, "get_temporal_client", new=AsyncMock(return_value=fake_temporal_client)
        ),
    ):
        with _client(user) as client:
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
    user = _make_current_user()
    operator = _make_operator(user.id)
    company = _make_company()
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
            jobs_module.OperatorRepository,
            "get_by_supabase_id",
            new=AsyncMock(return_value=operator),
        ),
        patch.object(
            jobs_module.CompanyRepository, "get_by_id", new=AsyncMock(return_value=company)
        ),
        patch.object(
            jobs_module.JobRepository, "create", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(
            jobs_module, "get_temporal_client", new=AsyncMock(return_value=fake_temporal_client)
        ),
    ):
        with _client(user) as client:
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
