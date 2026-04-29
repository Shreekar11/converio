"""RecruiterIndexingWorkflow integration tests (time-skipping environment).

Mirrors the candidate-side `test_candidate_indexing_workflow.py` exactly:
in-process activity stubs registered under production names, no Postgres,
Neo4j, or external HTTP calls. Each test owns a fresh `WorkflowEnvironment`.

Cases:
    1. Happy path — full pipeline → status='active', credibility >= 0.5.
    2. Low credibility — score < 0.5 → status='pending'.
    3. Re-run idempotency — same recruiter_id, same input twice; both runs
       return the same result.
"""
from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.schemas.enums import (
    CompanyStage,
    RecruitedFundingStage,
    RoleCategory,
    WorkspaceType,
)
from app.schemas.product.recruiter import (
    RecruiterClientItem,
    RecruiterIndexingInput,
    RecruiterPlacementItem,
    RecruiterProfile,
)
from app.temporal.product.recruiter_indexing.workflows.recruiter_indexing_workflow import (
    RecruiterIndexingWorkflow,
)
from tests.temporal.conftest import TEST_TASK_QUEUE


# ---------- Fixtures (kept local — recruiter side has its own profile shape) ----------

RECRUITER_ID = "33333333-3333-3333-3333-333333333333"


def _good_recruiter_profile(recruiter_id: str = RECRUITER_ID) -> RecruiterProfile:
    return RecruiterProfile(
        recruiter_id=recruiter_id,
        full_name="Pat Recruiter",
        email="pat@example.com",
        bio="A decade placing senior engineers at Series-A SaaS companies.",
        linkedin_url="https://linkedin.com/in/pat",
        domain_expertise=[RoleCategory.ENGINEERING, RoleCategory.DATA],
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SERIES_A,
        past_clients=[
            RecruiterClientItem(client_company_name="Stripe", role_focus=["backend"])
        ],
        past_placements=[
            RecruiterPlacementItem(
                candidate_name="Alice",
                company_name="Stripe",
                company_stage=CompanyStage.SERIES_A,
                role_title="Senior Engineer",
            ),
            RecruiterPlacementItem(
                candidate_name="Bob",
                company_name="Notion",
                company_stage=CompanyStage.SEED,
                role_title="Staff Engineer",
            ),
            RecruiterPlacementItem(
                candidate_name="Carol",
                company_name="Linear",
                company_stage=CompanyStage.SERIES_B,
                role_title="Principal Engineer",
            ),
        ],
    )


# ---------- Stub factory ----------


def _build_stubs(
    *,
    profile_dict: dict,
    dedup_result: dict,
    metrics_result: dict,
    persist_result: dict,
    graph_result: dict,
    credibility_result: dict,
) -> list:
    """Build a fresh set of activity stubs for one test case.

    Each stub takes the same arguments as the production activity, named so
    Temporal's `workflow.execute_activity(<func>, ...)` resolves the call to
    the stub via the `__name__` of the wrapped function.
    """

    @activity.defn(name="resolve_recruiter_duplicates")
    async def resolve_stub(profile_data: dict) -> dict:  # noqa: ARG001
        return dedup_result

    @activity.defn(name="compute_placement_metrics")
    async def metrics_stub(recruiter_id: str) -> dict:  # noqa: ARG001
        return metrics_result

    @activity.defn(name="generate_recruiter_embedding")
    async def embed_stub(profile_data: dict) -> dict:  # noqa: ARG001
        return {"embedding": [0.1] * 384}

    @activity.defn(name="persist_recruiter_record")
    async def persist_stub(
        recruiter_id: str,  # noqa: ARG001
        embedding: list,  # noqa: ARG001
        metrics_data: dict,  # noqa: ARG001
    ) -> dict:
        return persist_result

    @activity.defn(name="index_recruiter_to_graph")
    async def graph_stub(
        recruiter_id: str,  # noqa: ARG001
        profile_data: dict,  # noqa: ARG001
        metrics_data: dict,  # noqa: ARG001
    ) -> dict:
        return graph_result

    @activity.defn(name="score_recruiter_credibility")
    async def score_stub(
        recruiter_id: str,  # noqa: ARG001
        profile_data: dict,  # noqa: ARG001
    ) -> dict:
        return credibility_result

    return [
        resolve_stub,
        metrics_stub,
        embed_stub,
        persist_stub,
        graph_stub,
        score_stub,
    ]


def _build_input(profile: RecruiterProfile, source: str = "seed") -> dict:
    return RecruiterIndexingInput(profile=profile, source=source).model_dump(mode="json")


# ---------- Case 1: Happy path ----------


async def test_happy_path_recruiter_indexed() -> None:
    """Complete profile resolves to status='active' with credibility >= 0.5."""
    profile = _good_recruiter_profile()
    activities = _build_stubs(
        profile_dict=profile.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": True,
            "existing_recruiter_id": RECRUITER_ID,
            "match_source": "email",
        },
        metrics_result={
            "fill_rate_pct": 80.0,
            "avg_days_to_close": 25,
            "total_placements": 3,
            "placements_by_stage": {"series_a": 1, "seed": 1, "series_b": 1},
        },
        persist_result={"recruiter_id": RECRUITER_ID},
        graph_result={"nodes_merged": 6, "edges_merged": 6},
        credibility_result={
            "credibility_score": 0.95,
            "status": "active",
            "review_required": False,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterIndexingWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                RecruiterIndexingWorkflow.run,
                _build_input(profile),
                id=f"test-recruiter-wf-happy-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["status"] == "active"
    assert result["credibility_score"] == 0.95
    assert result["credibility_score"] >= 0.5
    assert result["recruiter_id"] == RECRUITER_ID
    assert result["source"] == "seed"


# ---------- Case 2: Low credibility -> pending ----------


async def test_low_credibility_routes_to_pending() -> None:
    """credibility_score < 0.5 → status='pending'."""
    profile = _good_recruiter_profile()
    activities = _build_stubs(
        profile_dict=profile.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": True,
            "existing_recruiter_id": RECRUITER_ID,
            "match_source": "email",
        },
        metrics_result={
            "fill_rate_pct": None,
            "avg_days_to_close": None,
            "total_placements": 0,
            "placements_by_stage": {},
        },
        persist_result={"recruiter_id": RECRUITER_ID},
        graph_result={"nodes_merged": 1, "edges_merged": 0},
        credibility_result={
            "credibility_score": 0.3,
            "status": "pending",
            "review_required": True,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterIndexingWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                RecruiterIndexingWorkflow.run,
                _build_input(profile, source="onboarding"),
                id=f"test-recruiter-wf-pending-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

    assert result["status"] == "pending"
    assert result["credibility_score"] == 0.3
    assert result["recruiter_id"] == RECRUITER_ID
    assert result["source"] == "onboarding"


# ---------- Case 3: Re-run idempotency ----------


async def test_rerun_with_same_recruiter_returns_same_result() -> None:
    """Two consecutive runs with the same recruiter_id + payload return identical results.

    Idempotency at the data layer is exercised in the activity tests (Neo4j MERGE
    counts, PG upsert preservation). Here we assert the workflow itself produces
    a deterministic result on re-invocation — guards against accidental nondeterminism
    in activity ordering.
    """
    profile = _good_recruiter_profile()
    common_kwargs = dict(
        profile_dict=profile.model_dump(mode="json"),
        dedup_result={
            "is_duplicate": True,
            "existing_recruiter_id": RECRUITER_ID,
            "match_source": "email",
        },
        metrics_result={
            "fill_rate_pct": 80.0,
            "avg_days_to_close": 25,
            "total_placements": 3,
            "placements_by_stage": {"series_a": 2, "seed": 1},
        },
        persist_result={"recruiter_id": RECRUITER_ID},
        graph_result={"nodes_merged": 6, "edges_merged": 6},
        credibility_result={
            "credibility_score": 0.85,
            "status": "active",
            "review_required": False,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterIndexingWorkflow],
            activities=_build_stubs(**common_kwargs),
        ):
            first = await env.client.execute_workflow(
                RecruiterIndexingWorkflow.run,
                _build_input(profile),
                id=f"test-recruiter-rerun-a-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )
            second = await env.client.execute_workflow(
                RecruiterIndexingWorkflow.run,
                _build_input(profile),
                id=f"test-recruiter-rerun-b-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

    assert first == second
    assert first["status"] == "active"
    assert first["credibility_score"] == 0.85
