"""Workflow happy-path test for ScorecardGeneratorWorkflow (Agent 4).

Golden path: every rubric dimension comes out of the initial LLM scoring
call with confidence >= 0.65, so the confidence gate trips
`all_confident=True` and the reflective evidence-gathering loop is
skipped entirely.

We assert the public contract of `ScorecardWorkflowResult`:
  * `self_correction_triggered == False`
  * `dimensions_rescored == []`
  * `termination_reason == "all_dims_confident"`
  * `overall_match_score == Decimal("85.00")` (computed deterministically
    from the fake LLM scores + weights)
  * `tool_call_count == 0` (no reflective tool calls)

All LLM and DB interactions are mocked via in-process `@activity.defn`
stubs registered under the production `scorecard.*` / `core.*` activity
names. Uses `temporalio.testing.WorkflowEnvironment.start_time_skipping`
so the workflow runs end-to-end in a hermetic temporalite instance with
no wall-clock waits — matching the pattern in
`tests/temporal/test_recruiter_assignment_workflow.py`.

The test is auto-skipped (not failed) when `WorkflowEnvironment` cannot
be imported (e.g. CI without the temporalite test-server binary). The
scenario logic stays intact so the test can be un-skipped at any time.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from temporalio import activity

from tests.temporal.conftest import TEST_TASK_QUEUE


# ---------------------------------------------------------------------------
# Skip-on-environment guards (mirror sibling agent tests).
# ---------------------------------------------------------------------------

try:  # pragma: no cover — environment guard
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from app.temporal.product.scorecard.workflows.scorecard_workflow import (
        ScorecardGeneratorWorkflow,
    )

    _ENV_READY = True
    _SKIP_REASON = ""
except Exception as exc:  # pragma: no cover
    _ENV_READY = False
    _SKIP_REASON = f"WorkflowEnvironment unavailable: {exc!r}"


pytestmark = pytest.mark.skipif(
    not _ENV_READY,
    reason=_SKIP_REASON or "WorkflowEnvironment / workflow import failed",
)


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

JOB_ID = "55555555-5555-5555-5555-555555555555"
CANDIDATE_ID = "66666666-6666-6666-6666-666666666666"
RUBRIC_ID = "77777777-7777-7777-7777-777777777777"
SCORECARD_ID = "88888888-8888-8888-8888-888888888888"


# Three rubric dims, evenly weighted (sum = 1.0), all scored 85 with
# confidence 0.90 — so 85*1/3 + 85*1/3 + 85*1/3 = 85.00 exactly.
_THREE_DIMS_HIGH_CONFIDENCE = [
    {
        "name": "distributed_systems_depth",
        "score": 85,
        "confidence": 0.90,
        "weight": 1 / 3,
        "rationale": "Built Kafka at scale.",
        "evidence_limited": False,
        "citation": {"text": "Stripe — payments infra", "resolution_method": "placeholder"},
    },
    {
        "name": "language_depth",
        "score": 85,
        "confidence": 0.88,
        "weight": 1 / 3,
        "rationale": "Strong Python signal across commits.",
        "evidence_limited": False,
        "citation": {"text": "10k+ Python LOC", "resolution_method": "placeholder"},
    },
    {
        "name": "system_design_thinking",
        "score": 85,
        "confidence": 0.85,
        "weight": 1 / 3,
        "rationale": "Designed a multi-region failover system.",
        "evidence_limited": False,
        "citation": {"text": "RFC: Failover-2024", "resolution_method": "placeholder"},
    },
]


# ---------------------------------------------------------------------------
# Activity stubs
# ---------------------------------------------------------------------------


def _build_happy_path_stubs() -> list:
    """Return the full activity stub set for the happy path.

    Every activity returns a JSON-serializable dict matching the shape
    the real activity emits at `model_dump(mode="json")` time. The
    workflow consumes only the keys it actually reads, so omitted
    fields are fine.
    """

    @activity.defn(name="scorecard.get_candidate_profile")
    async def _get_candidate(payload: dict) -> dict:  # noqa: ARG001
        return {
            "candidate_id": CANDIDATE_ID,
            "full_name": "Alice Engineer",
            "github_username": "alice",
            "skills": ["Python", "Kafka", "Kubernetes"],
            "profile_text": "Resume text for Alice.",
            "enriched_data": {
                "full_name": "Alice Engineer",
                "seniority": "senior",
                "years_experience": 7,
                "location": "Berlin",
                "github_username": "alice",
                "resume_text": "Built distributed payments infra for 4y at Stripe.",
                "skills": [
                    {"name": "Python"},
                    {"name": "Kafka"},
                    {"name": "Kubernetes"},
                ],
                "work_history": [
                    {"role_title": "Staff Engineer", "company": "Stripe"}
                ],
            },
        }

    @activity.defn(name="scorecard.get_job_rubric")
    async def _get_job_rubric(payload: dict) -> dict:  # noqa: ARG001
        return {
            "job_id": JOB_ID,
            "rubric_id": RUBRIC_ID,
            "job_description": "Senior backend engineer.",
            "intake_notes": "Wants Kafka prod experience.",
            "dimensions": [
                {"name": d["name"], "weight": d["weight"], "description": d["rationale"]}
                for d in _THREE_DIMS_HIGH_CONFIDENCE
            ],
        }

    @activity.defn(name="scorecard.build_scoring_prompt")
    async def _build_prompt(payload: dict) -> dict:  # noqa: ARG001
        return {
            "prompt": "FAKE SCORING PROMPT",
            "candidate_summary": "Alice Engineer — senior, 7y.",
        }

    @activity.defn(name="scorecard.score_candidate_dimensions")
    async def _score(payload: dict) -> dict:  # noqa: ARG001
        return {
            "scorecard_output": {
                "dimensions": list(_THREE_DIMS_HIGH_CONFIDENCE),
                "strengths": ["Strong infra"],
                "red_flags": [],
            },
            "tokens_used": 1500,
            "cost_usd": 0.05,
        }

    @activity.defn(name="scorecard.check_confidence_gate")
    async def _gate(payload: dict) -> dict:  # noqa: ARG001
        # All three dims have conf >= 0.85 → all_confident.
        return {
            "low_confidence_dimensions": [],
            "all_confident": True,
            "low_conf_count": 0,
        }

    @activity.defn(name="scorecard.compute_overall_match_score")
    async def _compute(payload: dict) -> dict:  # noqa: ARG001
        return {
            "overall_match_score": "85.00",
            "weight_sum": "1.00",
        }

    @activity.defn(name="scorecard.resolve_citations")
    async def _resolve(payload: dict) -> dict:
        # Pass through the dims with the same citations.
        return {"dimensions": payload.get("dimensions", [])}

    @activity.defn(name="scorecard.persist_scorecard")
    async def _persist(payload: dict) -> dict:  # noqa: ARG001
        return {
            "scorecard_id": SCORECARD_ID,
            "updated_at": "2026-05-21T00:00:00Z",
        }

    # The workflow registers the LLM tool & mark_scorecard_done activities
    # for the worker but doesn't call them on the happy path (gate is
    # all-confident). We still register stubs so the worker boots cleanly.
    @activity.defn(name="scorecard.mark_scorecard_done")
    async def _mark(payload: dict) -> dict:
        return {"done": True, "reason": payload.get("reason", "all_dims_confident")}

    @activity.defn(name="core.llm_decide_next_tool")
    async def _decide(payload: dict) -> dict:  # noqa: ARG001
        # Should never be called on the happy path; if it is, terminate
        # the loop cleanly so the test fails on assertion, not on hang.
        return {
            "tool_name": "mark_scorecard_done",
            "args": {"reason": "all_dims_confident"},
            "reasoning": "fallback stub",
            "tokens_used": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
        }

    return [
        _get_candidate,
        _get_job_rubric,
        _build_prompt,
        _score,
        _gate,
        _compute,
        _resolve,
        _persist,
        _mark,
        _decide,
    ]


# ---------------------------------------------------------------------------
# Workflow input
# ---------------------------------------------------------------------------


def _build_input() -> dict:
    return {
        "job_id": JOB_ID,
        "candidate_id": CANDIDATE_ID,
        "rubric_id": RUBRIC_ID,
        "submission_id": None,
    }


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_happy_path_all_dims_confident() -> None:
    """Initial scoring fully confident → reflective loop skipped."""
    activities = _build_happy_path_stubs()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[ScorecardGeneratorWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                ScorecardGeneratorWorkflow.run,
                _build_input(),
                id=f"test-scorecard-happy-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )
            result = await handle.result()

    # --- Public contract assertions ---
    # No reflective loop iterations.
    assert result["self_correction_triggered"] is False
    assert result["dimensions_rescored"] == []
    assert result["tool_call_count"] == 0

    # Terminal reason matches the gate-bypass branch.
    assert result["termination_reason"] == "all_dims_confident"

    # Deterministic overall score is the Decimal serialized as a string.
    assert Decimal(result["overall_match_score"]) == Decimal("85.00")

    # Persistence completed and returned a scorecard_id.
    assert result["scorecard_id"] == SCORECARD_ID
