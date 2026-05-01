"""Unit tests for `persist_job_record` (C3) — DB layer mocked.

`async_session_maker`, `JobRepository`, and `RubricRepository` are patched at
the activity module so no real PG connection is needed. The session is a
`MagicMock` with async-context-manager support; `session.execute` returns a
mock whose `.scalar_one_or_none()` is configured per-test for the WorkflowRun
upsert path.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import Rubric  # noqa: F401 — used for isinstance() in test helpers
from app.schemas.enums import JobStatus
from app.temporal.product.job_intake.activities.persist_job_record import (
    persist_job_record,
)

_ACTIVITY_MODULE = "app.temporal.product.job_intake.activities.persist_job_record"


def _classification() -> dict:
    return {
        "role_category": "engineering",
        "seniority_level": "senior",
        "stage_fit": "series_a",
        "remote_onsite": "remote",
        "must_have_skills": ["python", "distributed_systems"],
        "nice_to_have_skills": ["rust"],
        "rationale": "Senior backend engineer.",
    }


def _rubric() -> dict:
    return {
        "dimensions": [
            {
                "name": "distributed_systems_depth",
                "description": "Depth of distributed systems experience.",
                "weight": 0.4,
                "evaluation_guidance": "Score 0-5 based on distributed systems depth.",
            },
            {
                "name": "full_stack_ownership",
                "description": "Ability to ship across the stack.",
                "weight": 0.3,
                "evaluation_guidance": "Score 0-5 based on full stack ownership.",
            },
            {
                "name": "communication",
                "description": "Clarity in async + verbal communication.",
                "weight": 0.2,
                "evaluation_guidance": "Score 0-5 based on communication.",
            },
            {
                "name": "startup_stage_fit",
                "description": "Comfort at the company's funding stage.",
                "weight": 0.1,
                "evaluation_guidance": "Score 0-5 based on startup stage fit.",
            },
        ],
        "rationale": "Calibrated weights.",
    }


def _payload(**overrides) -> dict:
    base = {
        "job_id": str(uuid.uuid4()),
        "classification": _classification(),
        "rubric": _rubric(),
        "workflow_id": "job-intake-test-00",
    }
    base.update(overrides)
    return base


class _FakeJob:
    """Plain attribute container substituting for a `Job` ORM row.

    Using a real `Job(...)` requires running SA's instrumentation registry,
    which forces a `db_engine` connection in the test session conftest. The
    activity only reads + writes attributes on the Job object — never invokes
    ORM machinery — so a plain object is sufficient and keeps the test hermetic.
    """

    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


def _make_job(job_id: uuid.UUID, **overrides) -> _FakeJob:
    """Build a Job-shaped attribute container with defaults the activity expects."""
    fields = dict(
        id=job_id,
        company_id=uuid.uuid4(),
        created_by=None,
        title="Founding Engineer",
        jd_text="Build the platform.",
        intake_notes=None,
        role_category=None,
        seniority_level=None,
        stage_fit=None,
        remote_onsite=None,
        location_text=None,
        must_have_skills=None,
        nice_to_have_skills=None,
        compensation_min=None,
        compensation_max=None,
        status=JobStatus.INTAKE.value,
        workflow_id="job-intake-test-00",
        extra=None,
        updated_at=None,
    )
    fields.update(overrides)
    return _FakeJob(**fields)


def _session_maker_patch(session_mock: MagicMock):
    """Build an `async_session_maker` patch that returns an async-context manager
    yielding the provided session mock."""

    @asynccontextmanager
    async def _ctx():
        yield session_mock

    return MagicMock(side_effect=lambda: _ctx())


def _make_session(workflow_run_existing: WorkflowRun | None = None) -> MagicMock:
    """Build a session mock used by the activity.

    Configures `.execute(...)` so that `scalar_one_or_none()` returns the
    optionally-provided WorkflowRun (the activity's only direct `select(...)`).
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=workflow_run_existing)
    session.execute = AsyncMock(return_value=execute_result)
    return session


async def test_happy_path_updates_job_and_inserts_rubric() -> None:
    """All updates applied, status -> recruiter_assignment, fresh rubric_id returned."""
    payload = _payload()
    job_uuid = uuid.UUID(payload["job_id"])
    job = _make_job(job_uuid)

    rubric_id = uuid.uuid4()

    def _flush_assigns_rubric_id():
        # Simulate what the DB would do post-flush: assign Rubric.id to the
        # row that was added in this transaction.
        added_rows = [call.args[0] for call in session.add.call_args_list]
        for row in added_rows:
            if isinstance(row, Rubric) and row.id is None:
                row.id = rubric_id

    job_repo = MagicMock()
    job_repo.get_by_id = AsyncMock(return_value=job)

    rubric_repo = MagicMock()
    rubric_repo.get_latest_for_job = AsyncMock()

    session = _make_session(workflow_run_existing=None)
    session.flush = AsyncMock(side_effect=_flush_assigns_rubric_id)

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        result = await persist_job_record(payload)

    assert result["job_id"] == payload["job_id"]
    assert result["rubric_id"] == str(rubric_id)
    assert result["rubric_version"] == 1

    # Job was mutated in place.
    assert job.role_category == "engineering"
    assert job.seniority_level == "senior"
    assert job.stage_fit == "series_a"
    assert job.remote_onsite == "remote"
    assert job.must_have_skills == ["python", "distributed_systems"]
    assert job.nice_to_have_skills == ["rust"]
    assert job.status == JobStatus.RECRUITER_ASSIGNMENT.value

    # Rubric row was added (and a WorkflowRun, since the WF row didn't exist).
    added_types = {type(call.args[0]).__name__ for call in session.add.call_args_list}
    assert "Rubric" in added_types
    assert "WorkflowRun" in added_types
    session.commit.assert_awaited()


async def test_missing_job_raises_runtime_error() -> None:
    """Activity contract: a missing Job row is a hard failure."""
    job_repo = MagicMock()
    job_repo.get_by_id = AsyncMock(return_value=None)
    rubric_repo = MagicMock()
    session = _make_session()

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        with pytest.raises(RuntimeError, match="not found"):
            await persist_job_record(_payload())

    session.commit.assert_not_awaited()


async def test_replay_safe_duplicate_insert_returns_existing_rubric_id() -> None:
    """Temporal replay re-runs persist; duplicate `(job_id, version=1)` raises
    IntegrityError → activity rolls back, re-fetches existing, returns its id."""
    payload = _payload()
    job_uuid = uuid.UUID(payload["job_id"])
    job = _make_job(job_uuid)

    existing_rubric = _FakeJob(  # reuse the plain-attribute container
        id=uuid.uuid4(),
        job_id=job_uuid,
        version=1,
        dimensions=_rubric()["dimensions"],
    )

    job_repo = MagicMock()
    # Two get_by_id calls: once before INSERT, once after rollback re-fetch.
    job_repo.get_by_id = AsyncMock(side_effect=[job, job])

    rubric_repo = MagicMock()
    rubric_repo.get_latest_for_job = AsyncMock(return_value=existing_rubric)

    session = _make_session(workflow_run_existing=None)
    # First flush (the rubric INSERT) raises IntegrityError; the WorkflowRun
    # INSERT path doesn't call flush — only commit — so this single side_effect is enough.
    session.flush = AsyncMock(
        side_effect=IntegrityError("duplicate", params=None, orig=Exception("dup"))
    )

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        result = await persist_job_record(payload)

    assert result["rubric_id"] == str(existing_rubric.id)
    assert result["rubric_version"] == 1
    session.rollback.assert_awaited()
    rubric_repo.get_latest_for_job.assert_awaited_once_with(job_uuid)
    # Status reapplied post-rollback.
    assert job.status == JobStatus.RECRUITER_ASSIGNMENT.value


async def test_status_transitions_from_intake_to_recruiter_assignment() -> None:
    """Concrete assertion on the status transition (mirrors design doc §6)."""
    payload = _payload()
    job_uuid = uuid.UUID(payload["job_id"])
    job = _make_job(job_uuid, status=JobStatus.INTAKE.value)

    job_repo = MagicMock()
    job_repo.get_by_id = AsyncMock(return_value=job)
    rubric_repo = MagicMock()
    rubric_repo.get_latest_for_job = AsyncMock()

    rubric_id = uuid.uuid4()

    def _flush_assigns_rubric_id():
        for call in session.add.call_args_list:
            row = call.args[0]
            if isinstance(row, Rubric) and row.id is None:
                row.id = rubric_id

    session = _make_session()
    session.flush = AsyncMock(side_effect=_flush_assigns_rubric_id)

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        # Sanity precondition.
        assert job.status == JobStatus.INTAKE.value
        await persist_job_record(payload)

    assert job.status == JobStatus.RECRUITER_ASSIGNMENT.value


async def test_existing_workflow_run_is_updated_in_place() -> None:
    """If a WorkflowRun row already exists (e.g. shared `record_workflow_run_start`
    fired earlier), the activity updates it in place rather than INSERT-ing."""
    payload = _payload()
    job_uuid = uuid.UUID(payload["job_id"])
    job = _make_job(job_uuid)

    existing_run = _FakeJob(  # reuse the plain-attribute container
        id=uuid.uuid4(),
        workflow_id=payload["workflow_id"],
        workflow_type="JobIntakeWorkflow",
        status="running",
        job_id=None,
        completed_at=None,
    )

    job_repo = MagicMock()
    job_repo.get_by_id = AsyncMock(return_value=job)
    rubric_repo = MagicMock()

    rubric_id = uuid.uuid4()

    def _flush_assigns_rubric_id():
        for call in session.add.call_args_list:
            row = call.args[0]
            if isinstance(row, Rubric) and row.id is None:
                row.id = rubric_id

    session = _make_session(workflow_run_existing=existing_run)
    session.flush = AsyncMock(side_effect=_flush_assigns_rubric_id)

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        await persist_job_record(payload)

    assert existing_run.status == "completed"
    assert existing_run.job_id == job_uuid
    # WorkflowRun row was NOT re-added — only the Rubric was.
    added_types = [type(call.args[0]).__name__ for call in session.add.call_args_list]
    assert added_types.count("WorkflowRun") == 0
    assert added_types.count("Rubric") == 1


async def test_invalid_job_id_raises_value_error() -> None:
    """Bad UUID surfaces as ValueError before opening a session."""
    with pytest.raises(ValueError, match="invalid job_id"):
        await persist_job_record(_payload(job_id="not-a-uuid"))


async def test_does_not_overwrite_preexisting_classification_fields() -> None:
    """If Job already has classification fields populated (e.g. operator pre-fill),
    the activity must NOT clobber them."""
    payload = _payload()
    job_uuid = uuid.UUID(payload["job_id"])
    job = _make_job(
        job_uuid,
        role_category="data",
        seniority_level="staff",
        must_have_skills=["sql"],
    )

    job_repo = MagicMock()
    job_repo.get_by_id = AsyncMock(return_value=job)
    rubric_repo = MagicMock()

    rubric_id = uuid.uuid4()

    def _flush_assigns_rubric_id():
        for call in session.add.call_args_list:
            row = call.args[0]
            if isinstance(row, Rubric) and row.id is None:
                row.id = rubric_id

    session = _make_session()
    session.flush = AsyncMock(side_effect=_flush_assigns_rubric_id)

    with (
        patch(f"{_ACTIVITY_MODULE}.async_session_maker", _session_maker_patch(session)),
        patch(f"{_ACTIVITY_MODULE}.JobRepository", return_value=job_repo),
        patch(f"{_ACTIVITY_MODULE}.RubricRepository", return_value=rubric_repo),
    ):
        await persist_job_record(payload)

    # Pre-existing values preserved; the LLM-derived values from `classification`
    # were ignored for these fields.
    assert job.role_category == "data"
    assert job.seniority_level == "staff"
    assert job.must_have_skills == ["sql"]
    # But `stage_fit` and `remote_onsite` were null and DID get filled.
    assert job.stage_fit == "series_a"
    assert job.remote_onsite == "remote"
    # And status still transitions.
    assert job.status == JobStatus.RECRUITER_ASSIGNMENT.value
