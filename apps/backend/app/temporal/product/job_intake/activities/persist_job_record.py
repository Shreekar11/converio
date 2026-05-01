"""C3 — `persist_job_record` activity.

Single-transaction writer that:
  1. UPDATE Job — fill classification fields if currently null, transition
     `status` to `recruiter_assignment`.
  2. INSERT Rubric v1 — first version of the evaluation rubric for this job.
     Wrapped in `try/except IntegrityError` so Temporal replay re-runs are
     idempotent: a duplicate `(job_id, version)` violation triggers a rollback
     + re-fetch of the existing row, returning its id unchanged.
  3. UPSERT WorkflowRun — observability mirror for SSE/polling. Implemented
     inline (not via the shared `record_workflow_run_*` helpers) because we
     need it inside the same transaction as the Job + Rubric writes; the
     shared helpers each open their own session.

Per the activity contract (§4.3): a missing Job row is a hard failure — the
intake API guarantees existence by INSERTing the row before `start_workflow`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import Rubric, WorkflowRun
from app.repositories.jobs import JobRepository
from app.repositories.rubrics import RubricRepository
from app.schemas.enums import JobStatus
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_WORKFLOW_TYPE = "JobIntakeWorkflow"


def _is_blank_list(value: list | None) -> bool:
    return value is None or len(value) == 0


@ActivityRegistry.register("job_intake", "persist_job_record")
@activity.defn(name="job_intake.persist_job_record")
async def persist_job_record(payload: dict) -> dict:
    """Update Job + insert Rubric v1 + upsert WorkflowRun in one transaction.

    Inputs (dict):
        job_id: str (UUID of pre-inserted Job row).
        classification: dict (RoleClassification JSON from C1).
        rubric: dict (EvaluationRubric JSON from C2).
        workflow_id: str (Temporal workflow id, used for WorkflowRun upsert).

    Returns:
        {"job_id": str, "rubric_id": str, "rubric_version": 1}.
    """
    job_id_raw = payload.get("job_id")
    classification = payload.get("classification")
    rubric = payload.get("rubric")
    workflow_id = payload.get("workflow_id")

    if not isinstance(job_id_raw, str) or not job_id_raw.strip():
        raise ValueError("persist_job_record: 'job_id' is required (str UUID)")
    if not isinstance(classification, dict):
        raise ValueError("persist_job_record: 'classification' must be a dict")
    if not isinstance(rubric, dict):
        raise ValueError("persist_job_record: 'rubric' must be a dict")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("persist_job_record: 'workflow_id' is required (str)")

    try:
        job_uuid = uuid.UUID(job_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"persist_job_record: invalid job_id UUID {job_id_raw!r}") from exc

    dimensions = rubric.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("persist_job_record: rubric 'dimensions' must be a non-empty list")

    LOGGER.info(
        "Persisting job record",
        extra={
            "job_id": job_id_raw,
            "workflow_id": workflow_id,
            "dimension_count": len(dimensions),
        },
    )

    async with async_session_maker() as session:
        job_repo = JobRepository(session)
        rubric_repo = RubricRepository(session)

        job = await job_repo.get_by_id(job_uuid)
        if job is None:
            raise RuntimeError(
                f"persist_job_record: Job {job_id_raw} not found — intake API "
                "must INSERT the row before starting the workflow"
            )

        # 1. UPDATE Job — fill classification fields where null/empty, transition status.
        if job.role_category is None and classification.get("role_category"):
            job.role_category = classification["role_category"]
        if job.seniority_level is None and classification.get("seniority_level"):
            job.seniority_level = classification["seniority_level"]
        if job.stage_fit is None and classification.get("stage_fit"):
            job.stage_fit = classification["stage_fit"]
        if job.remote_onsite is None and classification.get("remote_onsite"):
            job.remote_onsite = classification["remote_onsite"]
        if _is_blank_list(job.must_have_skills) and classification.get("must_have_skills"):
            job.must_have_skills = list(classification["must_have_skills"])
        if _is_blank_list(job.nice_to_have_skills) and classification.get("nice_to_have_skills"):
            job.nice_to_have_skills = list(classification["nice_to_have_skills"])

        previous_status = job.status
        job.status = JobStatus.RECRUITER_ASSIGNMENT.value
        job.updated_at = datetime.now(timezone.utc)

        # 2. INSERT Rubric v1 — replay-safe via IntegrityError catch.
        rubric_row = Rubric(job_id=job_uuid, version=1, dimensions=dimensions)
        session.add(rubric_row)
        rubric_id: uuid.UUID
        try:
            await session.flush()
            rubric_id = rubric_row.id
        except IntegrityError:
            await session.rollback()
            LOGGER.info(
                "Rubric v1 already exists for job — Temporal replay; reusing existing",
                extra={"job_id": job_id_raw},
            )
            # Re-fetch on a fresh repository binding (rollback dropped the previous flush).
            existing_rubric = await rubric_repo.get_latest_for_job(job_uuid)
            if existing_rubric is None:
                # Concurrent delete or genuine integrity bug: fail loud rather than silently swallow.
                raise RuntimeError(
                    f"persist_job_record: rubric INSERT raised IntegrityError but no "
                    f"existing rubric found for job {job_id_raw}"
                ) from None
            rubric_id = existing_rubric.id

            # Rollback wiped the Job UPDATE; reapply on the post-rollback session.
            job = await job_repo.get_by_id(job_uuid)
            if job is None:  # pragma: no cover — extremely unlikely, defensive
                raise RuntimeError(
                    f"persist_job_record: Job {job_id_raw} disappeared after rollback"
                )
            if job.role_category is None and classification.get("role_category"):
                job.role_category = classification["role_category"]
            if job.seniority_level is None and classification.get("seniority_level"):
                job.seniority_level = classification["seniority_level"]
            if job.stage_fit is None and classification.get("stage_fit"):
                job.stage_fit = classification["stage_fit"]
            if job.remote_onsite is None and classification.get("remote_onsite"):
                job.remote_onsite = classification["remote_onsite"]
            if _is_blank_list(job.must_have_skills) and classification.get("must_have_skills"):
                job.must_have_skills = list(classification["must_have_skills"])
            if _is_blank_list(job.nice_to_have_skills) and classification.get(
                "nice_to_have_skills"
            ):
                job.nice_to_have_skills = list(classification["nice_to_have_skills"])
            job.status = JobStatus.RECRUITER_ASSIGNMENT.value
            job.updated_at = datetime.now(timezone.utc)

        # 3. UPSERT WorkflowRun — same session/transaction for atomicity.
        wf_result = await session.execute(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        )
        wf_run = wf_result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if wf_run is None:
            wf_run = WorkflowRun(
                workflow_id=workflow_id,
                workflow_type=_WORKFLOW_TYPE,
                job_id=job_uuid,
                status="completed",
                started_at=now,
                completed_at=now,
            )
            session.add(wf_run)
        else:
            wf_run.status = "completed"
            wf_run.workflow_type = _WORKFLOW_TYPE
            wf_run.job_id = job_uuid
            wf_run.completed_at = now

        await session.commit()

    LOGGER.info(
        "Job record persisted",
        extra={
            "job_id": job_id_raw,
            "rubric_id": str(rubric_id),
            "rubric_version": 1,
            "previous_status": previous_status,
            "new_status": JobStatus.RECRUITER_ASSIGNMENT.value,
        },
    )

    return {
        "job_id": job_id_raw,
        "rubric_id": str(rubric_id),
        "rubric_version": 1,
    }
