"""G2 — CandidateIndexingWorkflow integration tests (time-skipping environment).

These tests exercise the workflow end-to-end using `WorkflowEnvironment.start_time_skipping()`
with in-process activity stubs. No external services (PG, Neo4j, GitHub, LLM) are touched.

Activity stubs are registered under the production activity names so the workflow's
`workflow.execute_activity(<func>, ...)` calls resolve to them. With `@activity.defn`
the registered name defaults to the wrapped function's `__name__` — we therefore
either name the stub functions identically or pass `name="..."` explicitly.

Cases covered:
    1. Happy path — new candidate, complete profile -> status="indexed".
    2. Duplicate — resolve_entity_duplicates returns is_duplicate=True ->
       persisted under existing_candidate_id, was_duplicate=True.
    3. Sparse profile — score_profile_completeness returns status="review_queue".
"""
from __future__ import annotations

import base64

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.schemas.product.candidate import CandidateIndexingInput
from app.temporal.product.candidate_indexing.workflows.candidate_indexing_workflow import (
    CandidateIndexingWorkflow,
)
from tests.temporal.conftest import (
    CANDIDATE_ID,
    EXISTING_CANDIDATE_ID,
    GOOD_GITHUB,
    GOOD_PROFILE,
    SPARSE_GITHUB,
    SPARSE_PROFILE,
    TEST_TASK_QUEUE,
)


# ---------- Stub factory ----------


def _build_stubs(
    *,
    profile_dict: dict,
    github_dict: dict,
    dedup_result: dict,
    persist_result: dict,
    completeness_result: dict,
) -> list:
    """Build a fresh set of activity stubs for one test case.

    Each stub has the production activity's `__name__` so Temporal routes the
    workflow's activity calls to the stub.
    """

    @activity.defn(name="parse_resume")
    async def parse_resume_stub(raw_bytes_b64: str, mime_type: str) -> dict:  # noqa: ARG001
        return profile_dict

    @activity.defn(name="fetch_github_signals")
    async def fetch_github_signals_stub(github_username: str | None) -> dict:  # noqa: ARG001
        return github_dict

    @activity.defn(name="infer_skill_depth")
    async def infer_skill_depth_stub(profile_data: dict, github_signals_data: dict) -> dict:  # noqa: ARG001
        # Echo the profile back unchanged — depth re-tagging is exercised in unit tests.
        return profile_data

    @activity.defn(name="resolve_entity_duplicates")
    async def resolve_entity_duplicates_stub(profile_data: dict) -> dict:  # noqa: ARG001
        return dedup_result

    @activity.defn(name="generate_embedding")
    async def generate_embedding_stub(profile_data: dict) -> dict:  # noqa: ARG001
        return {"embedding": [0.1] * 384}

    @activity.defn(name="persist_candidate_record")
    async def persist_candidate_record_stub(
        profile_data: dict,  # noqa: ARG001
        embedding: list,  # noqa: ARG001
        github_signals: dict,  # noqa: ARG001
        source: str,  # noqa: ARG001
        source_recruiter_id: str | None,  # noqa: ARG001
        existing_candidate_id: str | None,  # noqa: ARG001
    ) -> dict:
        return persist_result

    @activity.defn(name="index_candidate_to_graph")
    async def index_candidate_to_graph_stub(
        candidate_id: str,  # noqa: ARG001
        profile_data: dict,  # noqa: ARG001
        github_signals_data: dict,  # noqa: ARG001
    ) -> dict:
        return {"nodes_merged": 5, "edges_merged": 4}

    @activity.defn(name="score_profile_completeness")
    async def score_profile_completeness_stub(
        candidate_id: str,  # noqa: ARG001
        profile_data: dict,  # noqa: ARG001
        github_signals_data: dict,  # noqa: ARG001
    ) -> dict:
        return completeness_result

    return [
        parse_resume_stub,
        fetch_github_signals_stub,
        infer_skill_depth_stub,
        resolve_entity_duplicates_stub,
        generate_embedding_stub,
        persist_candidate_record_stub,
        index_candidate_to_graph_stub,
        score_profile_completeness_stub,
    ]


def _build_input() -> dict:
    return CandidateIndexingInput(
        raw_bytes_b64=base64.b64encode(b"fake-resume-bytes").decode(),
        mime_type="application/pdf",
        source="seed",
    ).model_dump(mode="json")


# ---------- G2 Case 1: Happy path ----------


async def test_happy_path_candidate_indexed() -> None:
    """New candidate with a complete profile resolves to status='indexed'."""
    activities = _build_stubs(
        profile_dict=GOOD_PROFILE.model_dump(mode="json"),
        github_dict=GOOD_GITHUB.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": False,
            "existing_candidate_id": None,
            "match_source": None,
        },
        persist_result={"candidate_id": CANDIDATE_ID, "was_insert": True},
        completeness_result={
            "completeness_score": 0.92,
            "status": "indexed",
            "review_required": False,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[CandidateIndexingWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                CandidateIndexingWorkflow.run,
                _build_input(),
                id="test-wf-happy",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["status"] == "indexed"
    assert result["was_duplicate"] is False
    assert result["candidate_id"] == CANDIDATE_ID
    assert result["completeness_score"] == 0.92
    assert result["source"] == "seed"


# ---------- G2 Case 2: Duplicate candidate ----------


async def test_duplicate_candidate_was_duplicate_true() -> None:
    """resolve_entity_duplicates flags a duplicate -> persist returns existing id, was_duplicate=True."""
    activities = _build_stubs(
        profile_dict=GOOD_PROFILE.model_dump(mode="json"),
        github_dict=GOOD_GITHUB.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": True,
            "existing_candidate_id": EXISTING_CANDIDATE_ID,
            "match_source": "dedup_hash",
        },
        # Persist updates the existing row instead of inserting.
        persist_result={"candidate_id": EXISTING_CANDIDATE_ID, "was_insert": False},
        completeness_result={
            "completeness_score": 0.92,
            "status": "indexed",
            "review_required": False,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[CandidateIndexingWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                CandidateIndexingWorkflow.run,
                _build_input(),
                id="test-wf-duplicate",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["was_duplicate"] is True
    assert result["candidate_id"] == EXISTING_CANDIDATE_ID
    assert result["status"] == "indexed"


# ---------- G2 Case 3: Low completeness -> review_queue ----------


async def test_low_completeness_routes_to_review_queue() -> None:
    """Sparse profile yields completeness < 0.5 -> status='review_queue'."""
    activities = _build_stubs(
        profile_dict=SPARSE_PROFILE.model_dump(mode="json"),
        github_dict=SPARSE_GITHUB.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": False,
            "existing_candidate_id": None,
            "match_source": None,
        },
        persist_result={"candidate_id": CANDIDATE_ID, "was_insert": True},
        completeness_result={
            "completeness_score": 0.3,
            "status": "review_queue",
            "review_required": True,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[CandidateIndexingWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                CandidateIndexingWorkflow.run,
                _build_input(),
                id="test-wf-review",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["status"] == "review_queue"
    assert result["completeness_score"] == 0.3
    assert result["was_duplicate"] is False
    assert result["candidate_id"] == CANDIDATE_ID
