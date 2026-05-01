"""D3 — JobIntakeWorkflow integration tests (time-skipping environment).

Mirrors `test_recruiter_indexing_workflow.py`: in-process activity stubs are
registered under the production activity names so the workflow's
`workflow.execute_activity("job_intake.<name>", ...)` calls resolve to them.
No Postgres / Neo4j / LLM is touched.

Cases:
    1. Happy path — full pipeline → status='recruiter_assignment', rubric_version=1.
    2. Classify failure — classify activity always raises → workflow fails after retries.
    3. Persist failure — persist activity always raises → workflow fails.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.schemas.enums import (
    CompanyStage,
    JobStatus,
    RemoteOnsite,
    RoleCategory,
    Seniority,
)
from app.schemas.product.job import JobIntakeInput
from app.temporal.product.job_intake.workflows.job_intake_workflow import (
    JobIntakeWorkflow,
)
from tests.temporal.conftest import TEST_TASK_QUEUE


# ---------- Fixtures ----------

JOB_ID = "44444444-4444-4444-4444-444444444444"
RUBRIC_ID = "55555555-5555-5555-5555-555555555555"


def _classification_dict() -> dict:
    return {
        "role_category": RoleCategory.ENGINEERING.value,
        "seniority_level": Seniority.SENIOR.value,
        "stage_fit": CompanyStage.SERIES_A.value,
        "remote_onsite": RemoteOnsite.REMOTE.value,
        "must_have_skills": ["distributed systems", "python"],
        "nice_to_have_skills": ["go", "kubernetes"],
        "rationale": "Senior backend role at growth-stage startup, generalist preferred.",
    }


def _rubric_dict() -> dict:
    return {
        "dimensions": [
            {
                "name": "distributed_systems_depth",
                "description": "Track record building and operating distributed systems.",
                "weight": 0.3,
                "evaluation_guidance": "Score 0-5 on production distributed-systems experience.",
            },
            {
                "name": "full_stack_ownership",
                "description": "Comfort owning a feature end to end across the stack.",
                "weight": 0.25,
                "evaluation_guidance": "Score 0-5 on demonstrated end-to-end ownership.",
            },
            {
                "name": "startup_stage_fit",
                "description": "Fit with the company's current stage and pace.",
                "weight": 0.25,
                "evaluation_guidance": "Score 0-5 on prior startup-stage relevance.",
            },
            {
                "name": "communication_clarity",
                "description": "Clear written and verbal communication.",
                "weight": 0.2,
                "evaluation_guidance": "Score 0-5 on clarity in async + sync communication.",
            },
        ],
        "rationale": "Weights tuned for a small generalist team building distributed infra.",
    }


def _build_input(job_id: str = JOB_ID) -> dict:
    return JobIntakeInput(
        job_id=job_id,
        title="Founding Engineer",
        jd_text="We are hiring a founding engineer to build distributed systems...",
        intake_notes="Small team, generalist preferred.",
        remote_onsite=RemoteOnsite.REMOTE,
    ).model_dump(mode="json")


# ---------- Stub factory ----------


def _build_stubs(
    *,
    classification: dict,
    rubric: dict,
    persist_result: dict,
    classify_raises: BaseException | None = None,
    rubric_raises: BaseException | None = None,
    persist_raises: BaseException | None = None,
) -> list:
    """Build a fresh set of activity stubs for one test case.

    Each stub is registered under the production dotted name
    (`job_intake.<activity>`) so the workflow's string-based
    `execute_activity` calls resolve correctly.
    """

    @activity.defn(name="job_intake.classify_role_type")
    async def classify_stub(payload: dict) -> dict:  # noqa: ARG001
        if classify_raises is not None:
            raise classify_raises
        return classification

    @activity.defn(name="job_intake.generate_evaluation_rubric")
    async def rubric_stub(payload: dict) -> dict:  # noqa: ARG001
        if rubric_raises is not None:
            raise rubric_raises
        return rubric

    @activity.defn(name="job_intake.persist_job_record")
    async def persist_stub(payload: dict) -> dict:  # noqa: ARG001
        if persist_raises is not None:
            raise persist_raises
        return persist_result

    return [classify_stub, rubric_stub, persist_stub]


# ---------- Case 1: Happy path ----------


async def test_workflow_happy_path() -> None:
    """Full pipeline produces JobIntakeResult with status=recruiter_assignment."""
    activities = _build_stubs(
        classification=_classification_dict(),
        rubric=_rubric_dict(),
        persist_result={
            "job_id": JOB_ID,
            "rubric_id": RUBRIC_ID,
            "rubric_version": 1,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[JobIntakeWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                JobIntakeWorkflow.run,
                _build_input(),
                id=f"test-job-intake-happy-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["job_id"] == JOB_ID
    assert result["rubric_id"] == RUBRIC_ID
    assert result["rubric_version"] == 1
    assert result["status"] == JobStatus.RECRUITER_ASSIGNMENT.value
    assert result["status"] == "recruiter_assignment"


# ---------- Case 2: Classify failure propagates ----------


async def test_workflow_classify_failure_propagates() -> None:
    """If classify_role_type always raises, workflow fails after exhausting retries."""
    activities = _build_stubs(
        classification=_classification_dict(),
        rubric=_rubric_dict(),
        persist_result={"job_id": JOB_ID, "rubric_id": RUBRIC_ID, "rubric_version": 1},
        classify_raises=RuntimeError("LLM unavailable"),
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[JobIntakeWorkflow],
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    JobIntakeWorkflow.run,
                    _build_input(),
                    id=f"test-job-intake-classify-fail-{uuid.uuid4()}",
                    task_queue=TEST_TASK_QUEUE,
                )


# ---------- Case 3: Persist failure propagates ----------


async def test_workflow_persist_failure_propagates() -> None:
    """If persist_job_record always raises, workflow fails after exhausting retries."""
    activities = _build_stubs(
        classification=_classification_dict(),
        rubric=_rubric_dict(),
        persist_result={"job_id": JOB_ID, "rubric_id": RUBRIC_ID, "rubric_version": 1},
        persist_raises=RuntimeError("DB connection lost"),
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[JobIntakeWorkflow],
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    JobIntakeWorkflow.run,
                    _build_input(),
                    id=f"test-job-intake-persist-fail-{uuid.uuid4()}",
                    task_queue=TEST_TASK_QUEUE,
                )
