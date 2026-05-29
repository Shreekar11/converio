"""Workflow budget-exhaustion test for ScorecardGeneratorWorkflow.

The LLM never emits `mark_scorecard_done` — it keeps calling
`select_evidence_source` / a fetcher / `rescore_dimension` without
ever improving confidence. The workflow's Budget hits the hard
`tool_calls=8` ceiling and injects
`mark_scorecard_done(reason="budget_exhausted")` to break the loop
deterministically.

Asserted contract:
  * `termination_reason == "budget_exhausted"` (workflow-injected, not
    LLM-emitted — that reason is *reserved* for this branch per plan §11
    and the mark_scorecard_done input schema rejects it from the LLM).
  * `tool_call_count <= 8` — bounded by the hard budget ceiling. The
    workflow's pre-iteration check fires AFTER charging, so the exact
    final count is implementation-dependent; <= 8 is the contract.
  * `self_correction_triggered is True` — loop ran at least once.
  * Scorecard was still persisted (best-effort partial output). The
    workflow must not abort mid-pipeline on budget exhaustion.

Same skip-on-env guard as the sibling workflow tests.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity

from tests.temporal.conftest import TEST_TASK_QUEUE


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

_DIM_NAMES = ("dim_a", "dim_b", "dim_c")

# All three dims stuck at 0.40 confidence — well below the 0.65 gate,
# and the rescore stub will keep them at 0.40 so the loop never
# converges on its own.
_STUCK_DIMS = [
    {
        "name": name,
        "score": 50,
        "confidence": 0.40,
        "weight": 1 / 3,
        "rationale": f"Initial rationale for {name}.",
        "evidence_limited": False,
        "citation": {"text": "n/a", "resolution_method": "placeholder"},
    }
    for name in _DIM_NAMES
]


# ---------------------------------------------------------------------------
# LLM decision sequencer — pathological: never emits mark_scorecard_done.
# ---------------------------------------------------------------------------


def _make_llm_decide_pathological():
    """Decision stub that always picks `select_evidence_source` for dim_a.

    Cycles through {select, fetch, rescore} forever — the workflow must
    detect the budget ceiling and inject termination. Returns a small
    decision cost so the budget is dominated by tool_call_count.
    """
    # Cycle the tool name so the budget burns through diverse tools.
    sequence = [
        ("select_evidence_source", {"low_conf_dim": {"name": _DIM_NAMES[0]}}),
        ("fetch_repo_readmes", {"github_username": "carol"}),
        ("rescore_dimension", {"dimension_name": _DIM_NAMES[0]}),
    ]
    counter = {"i": 0}

    @activity.defn(name="core.llm_decide_next_tool")
    async def _decide(payload: dict) -> dict:  # noqa: ARG001
        tool_name, args = sequence[counter["i"] % len(sequence)]
        counter["i"] += 1
        return {
            "tool_name": tool_name,
            "args": args,
            "reasoning": "pathological mock — never terminates",
            "tokens_used": 100,
            "cost_usd": 0.001,
            "latency_ms": 50,
        }

    return _decide


# ---------------------------------------------------------------------------
# Activity stubs
# ---------------------------------------------------------------------------


def _build_stuck_stubs() -> list:
    @activity.defn(name="scorecard.get_candidate_profile")
    async def _get_candidate(payload: dict) -> dict:  # noqa: ARG001
        return {
            "candidate_id": CANDIDATE_ID,
            "full_name": "Carol Engineer",
            "github_username": "carol",
            "skills": [],
            "profile_text": "Resume text for Carol.",
            "enriched_data": {
                "full_name": "Carol Engineer",
                "github_username": "carol",
                "skills": [],
                "work_history": [],
            },
        }

    @activity.defn(name="scorecard.get_job_rubric")
    async def _get_job_rubric(payload: dict) -> dict:  # noqa: ARG001
        return {
            "job_id": JOB_ID,
            "rubric_id": RUBRIC_ID,
            "job_description": "Senior backend engineer.",
            "intake_notes": None,
            "dimensions": [
                {"name": n, "weight": 1 / 3, "description": "test dim"}
                for n in _DIM_NAMES
            ],
        }

    @activity.defn(name="scorecard.build_scoring_prompt")
    async def _build_prompt(payload: dict) -> dict:  # noqa: ARG001
        return {
            "prompt": "FAKE SCORING PROMPT",
            "candidate_summary": "Carol Engineer.",
        }

    @activity.defn(name="scorecard.score_candidate_dimensions")
    async def _score(payload: dict) -> dict:  # noqa: ARG001
        return {
            "scorecard_output": {
                "dimensions": list(_STUCK_DIMS),
                "strengths": [],
                "red_flags": [],
            },
            "tokens_used": 1500,
            "cost_usd": 0.05,
        }

    @activity.defn(name="scorecard.check_confidence_gate")
    async def _gate(payload: dict) -> dict:  # noqa: ARG001
        # All three dims below threshold; loop must fire.
        return {
            "low_confidence_dimensions": [
                {
                    "name": n,
                    "current_confidence": 0.40,
                    "current_score": 50,
                    "reason": "confidence 0.40 below threshold 0.65",
                }
                for n in _DIM_NAMES
            ],
            "all_confident": False,
            "low_conf_count": 3,
        }

    @activity.defn(name="scorecard.select_evidence_source")
    async def _select(payload: dict) -> dict:  # noqa: ARG001
        # Always pick fetch_repo_readmes — pathological repeat behaviour.
        return {
            "tool_name": "fetch_repo_readmes",
            "reasoning": "pathological mock — same fetcher every time",
        }

    @activity.defn(name="scorecard.fetch_repo_readmes")
    async def _fetch_readmes(payload: dict) -> dict:  # noqa: ARG001
        # Empty result — no evidence available. Rescore will be a no-op.
        return {"repos": [], "total_count": 0}

    @activity.defn(name="scorecard.fetch_commit_history")
    async def _fetch_commits(payload: dict) -> dict:  # noqa: ARG001
        return {
            "total_commits_last_year": 0,
            "commits_by_month": {},
            "dominant_language": None,
            "active_months_in_window": 0,
        }

    @activity.defn(name="scorecard.fetch_pr_review_history")
    async def _fetch_reviews(payload: dict) -> dict:  # noqa: ARG001
        return {"reviews": [], "total_count": 0}

    @activity.defn(name="scorecard.fetch_repo_languages")
    async def _fetch_langs(payload: dict) -> dict:  # noqa: ARG001
        return {"languages": {}, "primary_language": None, "language_count": 0}

    @activity.defn(name="scorecard.fetch_user_orgs_and_stars")
    async def _fetch_orgs(payload: dict) -> dict:  # noqa: ARG001
        return {"organizations": [], "total_starred_repos": 0, "notable_repos_starred": []}

    @activity.defn(name="scorecard.rescore_dimension")
    async def _rescore(payload: dict) -> dict:
        # Confidence DOES NOT improve — keep dim stuck so the LLM (or
        # rather, the pathological stub) never satisfies the gate.
        dim_name = payload.get("dimension_name", _DIM_NAMES[0])
        return {
            "new_score": 50,
            "new_confidence": 0.40,
            "new_rationale": f"No new evidence for {dim_name}.",
            "new_citation_text": None,
            "evidence_hash": "empty",
            "tokens_used": 800,
            "cost_usd": 0.03,
        }

    @activity.defn(name="scorecard.mark_scorecard_done")
    async def _mark(payload: dict) -> dict:
        return {"done": True, "reason": payload.get("reason", "budget_exhausted")}

    @activity.defn(name="scorecard.compute_overall_match_score")
    async def _compute(payload: dict) -> dict:  # noqa: ARG001
        # Even on budget exhaustion the workflow still computes an
        # overall — the persisted scorecard is best-effort, not aborted.
        return {"overall_match_score": "50.00", "weight_sum": "1.00"}

    @activity.defn(name="scorecard.resolve_citations")
    async def _resolve(payload: dict) -> dict:
        return {"dimensions": payload.get("dimensions", [])}

    @activity.defn(name="scorecard.persist_scorecard")
    async def _persist(payload: dict) -> dict:  # noqa: ARG001
        return {"scorecard_id": SCORECARD_ID, "updated_at": "2026-05-21T00:00:00Z"}

    return [
        _get_candidate,
        _get_job_rubric,
        _build_prompt,
        _score,
        _gate,
        _select,
        _fetch_readmes,
        _fetch_commits,
        _fetch_reviews,
        _fetch_langs,
        _fetch_orgs,
        _rescore,
        _mark,
        _compute,
        _resolve,
        _persist,
    ]


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


async def test_budget_exhaustion_terminates_loop() -> None:
    """LLM never terminates → budget exhausts → workflow still persists."""
    activities = [_make_llm_decide_pathological()]
    activities.extend(_build_stuck_stubs())

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
                id=f"test-scorecard-budget-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )
            result = await handle.result()

    # Loop ran — `self_correction_triggered` flips true the moment the
    # workflow enters the reflective branch.
    assert result["self_correction_triggered"] is True

    # The workflow injects this reason; it is reserved and may NOT come
    # from the LLM (mark_scorecard_done's pydantic input rejects it).
    assert result["termination_reason"] == "budget_exhausted"

    # Bounded by the hard ceiling — the budget short-circuits *before*
    # the next iteration runs, so the final count is <= 8.
    assert result["tool_call_count"] <= 8

    # Critical: scorecard is still persisted. Budget exhaustion is a
    # best-effort outcome, not a workflow abort.
    assert result["scorecard_id"] == SCORECARD_ID
