"""Hermetic endpoint tests for the Recruiter Assignment HITL API surface.

Mirrors the strategy in `test_jobs_endpoints.py`:
  * The DB session, repositories, and Temporal client are all mocked.
    No PG, no Temporal, no Supabase — these tests run in-process.
  * We mount only the `recruiter_assignment` router under `/jobs` on a
    minimal FastAPI app. `get_current_operator` and `get_async_session`
    are overridden via FastAPI's dep-override mechanism.

Endpoints under test:
  GET  /jobs/{job_id}/recruiter-assignment/proposal
  POST /jobs/{job_id}/recruiter-assignment/approve
  GET  /jobs/{job_id}/recruiter-assignment/status
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from app.api.v1.endpoints import recruiter_assignment as ra_module
from app.core.auth import get_current_operator
from app.core.database import get_async_session
from app.database.models import Operator


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_operator(operator_id: uuid.UUID | None = None) -> Operator:
    return Operator(
        id=operator_id or uuid.uuid4(),
        supabase_user_id=f"sup-{uuid.uuid4().hex[:8]}",
        email="op@converio.example",
        full_name="Test Operator",
        status="active",
    )


def _make_proposal(
    *,
    job_id: uuid.UUID | None = None,
    workflow_id: str = "wf-test",
    quality_flag: str = "high",
    payload: dict | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like an OperatorProposal ORM row.

    We use MagicMock rather than instantiating the SQLAlchemy model so the
    tests don't depend on declarative metadata being importable in
    isolation.
    """
    proposal = MagicMock()
    proposal.id = uuid.uuid4()
    proposal.job_id = job_id or uuid.uuid4()
    proposal.workflow_id = workflow_id
    proposal.quality_flag = quality_flag
    proposal.payload = payload or {
        "job_id": str(proposal.job_id),
        "workflow_id": workflow_id,
        "proposed_recruiter_ids": ["r1", "r2"],
        "candidates": [],
        "fit_scores": [],
        "narratives": {},
        "audit_trail": [],
        "quality_flag": quality_flag,
        "total_tool_calls": 5,
        "total_tokens": 1234,
        "total_cost_usd": 0.05,
    }
    proposal.created_at = datetime.now(tz=timezone.utc)
    return proposal


def _build_app(
    operator: Operator | None = None,
    *,
    proposal: Any | None = None,
    forbid: bool = False,
) -> FastAPI:
    """Mount the recruiter_assignment router and override deps.

    Args:
        operator: returned by the get_current_operator dep. Required unless
            `forbid=True`.
        proposal: returned by `OperatorProposalRepository.fetch_latest_for_job`.
            None means "no proposal exists" (-> 404 from the endpoint).
        forbid: if True, the operator dep raises 403 (mirrors the production
            behaviour for non-operator callers).
    """
    app = FastAPI()
    app.include_router(ra_module.router, prefix="/jobs")

    if forbid:
        async def _override_operator() -> Operator:  # noqa: RUF029
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operator privileges required",
            )
    else:
        assert operator is not None, "operator must be provided when forbid=False"

        async def _override_operator() -> Operator:
            return operator

    async def _override_session():
        yield AsyncMock()

    app.dependency_overrides[get_current_operator] = _override_operator
    app.dependency_overrides[get_async_session] = _override_session

    # Patch the repository's fetch method to return whatever the test wants.
    # We patch on the module under test so the patch is naturally scoped to
    # the test client's lifetime.
    app.state.proposal = proposal
    return app


def _patch_repo(proposal: Any | None):
    """Return an async-mock context manager for the proposal repo lookup."""
    return patch.object(
        ra_module.OperatorProposalRepository,
        "fetch_latest_for_job",
        new=AsyncMock(return_value=proposal),
    )


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/recruiter-assignment/proposal
# ---------------------------------------------------------------------------


def test_get_proposal_200() -> None:
    operator = _make_operator()
    job_id = uuid.uuid4()
    proposal = _make_proposal(job_id=job_id)

    app = _build_app(operator=operator, proposal=proposal)

    with _patch_repo(proposal):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/proposal")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["status"] is True
    data = body["data"]
    assert data["proposal_id"] == str(proposal.id)
    assert data["job_id"] == str(proposal.job_id)
    assert data["workflow_id"] == proposal.workflow_id
    assert data["quality_flag"] == proposal.quality_flag
    assert data["payload"] == proposal.payload


def test_get_proposal_404() -> None:
    operator = _make_operator()
    job_id = uuid.uuid4()

    app = _build_app(operator=operator, proposal=None)

    with _patch_repo(None):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/proposal")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "No proposal found for this job"


def test_get_proposal_403() -> None:
    """Non-operator caller -> 403 from the operator dep, no DB access."""
    job_id = uuid.uuid4()
    app = _build_app(forbid=True)

    repo_fetch = AsyncMock()
    with patch.object(
        ra_module.OperatorProposalRepository,
        "fetch_latest_for_job",
        new=repo_fetch,
    ):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/proposal")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    repo_fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/recruiter-assignment/approve
# ---------------------------------------------------------------------------


def test_post_approve_202() -> None:
    operator = _make_operator()
    job_id = uuid.uuid4()
    proposal = _make_proposal(job_id=job_id, workflow_id="wf-approve")

    fake_handle = MagicMock()
    fake_handle.signal = AsyncMock()
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    app = _build_app(operator=operator, proposal=proposal)
    body = {
        "decision": "approve",
        "confirmed_recruiter_ids": ["r1", "r2"],
        "override_set": [],
        "notes": None,
    }

    with (
        _patch_repo(proposal),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post(
                f"/jobs/{job_id}/recruiter-assignment/approve", json=body
            )

    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    rj = resp.json()
    assert rj["status"] is True
    data = rj["data"]
    assert data["job_id"] == str(job_id)
    assert data["decision"] == "approve"
    assert data["workflow_id"] == "wf-approve"

    # Workflow handle was signaled with the operator id sourced from auth.
    fake_temporal_client.get_workflow_handle.assert_called_once_with("wf-approve")
    fake_handle.signal.assert_awaited_once()
    sig_args = fake_handle.signal.await_args
    assert sig_args.args[0] == "operator_approval"
    sent_payload = sig_args.args[1]
    assert sent_payload["decision"] == "approve"
    assert sent_payload["operator_id"] == str(operator.id)
    # Body's confirmed_recruiter_ids passed through untouched.
    assert sent_payload["confirmed_recruiter_ids"] == ["r1", "r2"]


def test_post_approve_422_bad_body() -> None:
    """`decision` outside the allowed set -> 422, no signal sent."""
    operator = _make_operator()
    job_id = uuid.uuid4()

    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock()

    app = _build_app(operator=operator, proposal=_make_proposal(job_id=job_id))

    with (
        _patch_repo(_make_proposal(job_id=job_id)),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post(
                f"/jobs/{job_id}/recruiter-assignment/approve",
                json={"decision": "maybe"},
            )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    fake_temporal_client.get_workflow_handle.assert_not_called()


def test_post_approve_403() -> None:
    """Non-operator -> 403 before DB or Temporal IO."""
    job_id = uuid.uuid4()
    app = _build_app(forbid=True)

    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock()
    repo_fetch = AsyncMock()

    with (
        patch.object(
            ra_module.OperatorProposalRepository,
            "fetch_latest_for_job",
            new=repo_fetch,
        ),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post(
                f"/jobs/{job_id}/recruiter-assignment/approve",
                json={
                    "decision": "approve",
                    "confirmed_recruiter_ids": ["r1"],
                    "override_set": [],
                    "notes": None,
                },
            )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    repo_fetch.assert_not_awaited()
    fake_temporal_client.get_workflow_handle.assert_not_called()


def test_post_approve_404_no_proposal() -> None:
    """Operator authenticated but no proposal exists -> 404."""
    operator = _make_operator()
    job_id = uuid.uuid4()

    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock()

    app = _build_app(operator=operator, proposal=None)

    with (
        _patch_repo(None),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post(
                f"/jobs/{job_id}/recruiter-assignment/approve",
                json={
                    "decision": "approve",
                    "confirmed_recruiter_ids": ["r1"],
                    "override_set": [],
                    "notes": None,
                },
            )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    fake_temporal_client.get_workflow_handle.assert_not_called()


def test_post_approve_502_temporal_error() -> None:
    """Temporal signal raises -> generic 502, no internal detail leaked."""
    operator = _make_operator()
    job_id = uuid.uuid4()
    proposal = _make_proposal(job_id=job_id, workflow_id="wf-error")

    fake_handle = MagicMock()
    fake_handle.signal = AsyncMock(
        side_effect=RuntimeError(
            "temporal frontend unreachable: dial tcp 127.0.0.1:7233"
        )
    )
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    app = _build_app(operator=operator, proposal=proposal)

    with (
        _patch_repo(proposal),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.post(
                f"/jobs/{job_id}/recruiter-assignment/approve",
                json={
                    "decision": "approve",
                    "confirmed_recruiter_ids": ["r1"],
                    "override_set": [],
                    "notes": None,
                },
            )

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    detail = resp.json()["detail"]
    # Generic detail — no echo of the underlying exception text.
    assert "temporal frontend unreachable" not in detail
    assert "127.0.0.1" not in detail


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/recruiter-assignment/status
# ---------------------------------------------------------------------------


def test_get_status_200() -> None:
    operator = _make_operator()
    job_id = uuid.uuid4()
    proposal = _make_proposal(job_id=job_id, workflow_id="wf-status")

    fake_handle = MagicMock()
    fake_handle.query = AsyncMock(return_value="awaiting_operator")
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    app = _build_app(operator=operator, proposal=proposal)

    with (
        _patch_repo(proposal),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/status")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["status"] is True
    data = body["data"]
    assert data["job_id"] == str(job_id)
    assert data["workflow_id"] == "wf-status"
    assert data["phase"] == "awaiting_operator"

    fake_temporal_client.get_workflow_handle.assert_called_once_with("wf-status")
    fake_handle.query.assert_awaited_once_with("current_phase")


def test_get_status_404_no_proposal() -> None:
    operator = _make_operator()
    job_id = uuid.uuid4()

    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock()

    app = _build_app(operator=operator, proposal=None)

    with (
        _patch_repo(None),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/status")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    fake_temporal_client.get_workflow_handle.assert_not_called()


def test_get_status_502_temporal_error() -> None:
    """Workflow query raises -> generic 502."""
    operator = _make_operator()
    job_id = uuid.uuid4()
    proposal = _make_proposal(job_id=job_id, workflow_id="wf-status-err")

    fake_handle = MagicMock()
    fake_handle.query = AsyncMock(side_effect=RuntimeError("upstream Temporal blew up"))
    fake_temporal_client = MagicMock()
    fake_temporal_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    app = _build_app(operator=operator, proposal=proposal)

    with (
        _patch_repo(proposal),
        patch.object(
            ra_module,
            "get_temporal_client",
            new=AsyncMock(return_value=fake_temporal_client),
        ),
    ):
        with TestClient(app) as client:
            resp = client.get(f"/jobs/{job_id}/recruiter-assignment/status")

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    detail = resp.json()["detail"]
    assert "upstream Temporal blew up" not in detail
