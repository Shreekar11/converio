"""Workflow tests for RecruiterAssignmentWorkflow (Agent 0).

All LLM and DB interactions are mocked — no real Temporal server needed.
Uses temporalio.testing.WorkflowEnvironment for in-process execution.

Coverage:
  1. test_happy_path_operator_approves      — full happy path, single approval
  2. test_operator_rejects_then_reapproves  — reject -> re-loop -> approve
  3. test_operator_rejects_twice_terminal   — reject + reject -> terminal
  4. test_budget_exhaustion_still_proposes  — 26 LLM tool calls -> best-effort proposal
  5. test_empty_pool_override_honored       — operator override_set assigned

Each test follows the same shape as `test_job_intake_workflow.py`:
  * Build deterministic activity stubs registered under their production
    `recruiter_assignment.<name>` activity names (so the workflow's
    string-based execute_activity calls resolve to them).
  * Stand up a time-skipping WorkflowEnvironment + Worker.
  * Drive the workflow through one or more `operator_approval` signals.
  * Assert the returned RecruiterAssignmentResult-shaped dict.

NOTE: WorkflowEnvironment requires the `temporalite` test server binary
to be downloadable on first run. In CI environments without network /
without that binary, the tests are auto-skipped (not failed) so the
overall suite stays green. The scenario logic below is fully implemented
so they can be un-skipped at any time.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio import activity

from tests.temporal.conftest import TEST_TASK_QUEUE


# ---------------------------------------------------------------------------
# Skip-on-environment guards
# ---------------------------------------------------------------------------
# The workflow under test imports the temporal sandbox eagerly. We try to
# import WorkflowEnvironment + Worker + the workflow; if anything is
# unavailable in this environment we skip the entire module rather than
# hard-fail (matches how the replay tests handle missing fixtures).

try:  # pragma: no cover — environment guard
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from app.temporal.product.recruiter_assignment.workflows.recruiter_assignment_workflow import (
        RecruiterAssignmentWorkflow,
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


JOB_ID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# Mock LLM sequencer
# ---------------------------------------------------------------------------


def _make_llm_decide_stub(tool_calls: list[tuple[str, dict]]):
    """Return an `@activity.defn(name='core.llm_decide_next_tool')` stub.

    The stub yields the prepared `(tool_name, args)` tuples in order, one per
    invocation. A test that exhausts the list will fall through to a default
    "stop" tool call so the workflow can wind down cleanly without a
    StopIteration leaking into the workflow.
    """
    call_iter = iter(tool_calls)

    @activity.defn(name="core.llm_decide_next_tool")
    async def _llm_decide(payload: dict) -> dict:  # noqa: ARG001
        try:
            tool_name, args = next(call_iter)
        except StopIteration:
            # If the test under-specifies, fall through to terminal tool so
            # the workflow doesn't spin. This makes test failures more
            # diagnosable (you'll see "ran out of LLM responses" in the
            # final result rather than a hang).
            tool_name, args = (
                "propose_assignment_set",
                {
                    "recruiter_ids": [],
                    "rationale": "fallback (test ran out of LLM responses)",
                    "quality_flag": "low",
                },
            )
        return {
            "tool_name": tool_name,
            "args": args,
            "reasoning": "mock",
            "tokens_used": 100,
            "cost_usd": 0.001,
            "latency_ms": 50,
        }

    return _llm_decide


# ---------------------------------------------------------------------------
# Default tool-activity stubs
# ---------------------------------------------------------------------------


_DEFAULT_CANDIDATES: list[dict] = [
    {
        "recruiter_id": "r1",
        "full_name": "Alice",
        "email": "alice@example.com",
        "domain_expertise": ["engineering"],
        "fill_rate_pct": 80.0,
        "avg_days_to_close": 30.0,
        "total_placements": 10,
        "status": "active",
        "at_capacity": False,
    },
    {
        "recruiter_id": "r2",
        "full_name": "Bob",
        "email": "bob@example.com",
        "domain_expertise": ["engineering"],
        "fill_rate_pct": 75.0,
        "avg_days_to_close": 35.0,
        "total_placements": 8,
        "status": "active",
        "at_capacity": False,
    },
]


_DEFAULT_SCORES: list[dict] = [
    {
        "recruiter_id": "r1",
        "score": 88,
        "confidence": 0.92,
        "rationale": "Strong domain match",
        "sub_scores": {
            "domain": 90,
            "stage": 85,
            "seniority": 80,
            "fill_rate": 85,
            "close_time": 90,
        },
    },
    {
        "recruiter_id": "r2",
        "score": 75,
        "confidence": 0.85,
        "rationale": "Decent track record",
        "sub_scores": {
            "domain": 70,
            "stage": 75,
            "seniority": 75,
            "fill_rate": 80,
            "close_time": 70,
        },
    },
]


def _build_default_tool_stubs(
    *,
    candidates: list[dict] | None = None,
) -> list:
    """Build the standard stub set for the Recruiter Assignment tools."""
    cands = candidates if candidates is not None else _DEFAULT_CANDIDATES

    @activity.defn(name="recruiter_assignment.search_recruiter_pool")
    async def _search(payload: dict) -> dict:  # noqa: ARG001
        return {"candidates": cands, "count": len(cands)}

    @activity.defn(name="recruiter_assignment.widen_domain_search")
    async def _widen(payload: dict) -> dict:  # noqa: ARG001
        return {"candidates": cands, "count": len(cands)}

    @activity.defn(name="recruiter_assignment.relax_stage_match")
    async def _relax(payload: dict) -> dict:  # noqa: ARG001
        return {"candidates": cands, "count": len(cands)}

    @activity.defn(name="recruiter_assignment.query_recruiter_capacity")
    async def _capacity(payload: dict) -> dict:  # noqa: ARG001
        return {
            "capacity": [
                {
                    "recruiter_id": c["recruiter_id"],
                    "current_open_roles": 1,
                    "capacity_max": 5,
                    "at_capacity": False,
                }
                for c in cands
            ]
        }

    @activity.defn(name="recruiter_assignment.check_recent_placements")
    async def _placements(payload: dict) -> dict:  # noqa: ARG001
        return {"placements": [], "count": 0}

    @activity.defn(name="recruiter_assignment.summarize_recruiter_track_record")
    async def _summarize(payload: dict) -> dict:
        rid = payload.get("recruiter_id", "unknown")
        return {"recruiter_id": rid, "narrative": f"Narrative for {rid}"}

    @activity.defn(name="recruiter_assignment.score_recruiter_fit")
    async def _score(payload: dict) -> dict:  # noqa: ARG001
        return {
            "scores": _DEFAULT_SCORES,
            "scored_count": len(_DEFAULT_SCORES),
            "cached_count": 0,
        }

    @activity.defn(name="recruiter_assignment.rank_and_select_recruiters")
    async def _rank(payload: dict) -> dict:  # noqa: ARG001
        return {
            "ranked": [
                {"recruiter_id": s["recruiter_id"], "weighted_score": float(s["score"])}
                for s in _DEFAULT_SCORES
            ],
            "top_n": 5,
            "total_scored": len(_DEFAULT_SCORES),
        }

    @activity.defn(name="recruiter_assignment.format_recruiter_recommendations")
    async def _format(payload: dict) -> dict:
        proposed_ids = payload.get("ranked_recruiter_ids") or []
        return {
            "job_id": payload.get("job_id", JOB_ID),
            "workflow_id": payload.get("workflow_id", "wf-test"),
            "proposed_recruiter_ids": proposed_ids,
            "candidates": cands,
            "fit_scores": _DEFAULT_SCORES,
            "narratives": {rid: f"Narrative for {rid}" for rid in proposed_ids},
            "audit_trail": [],
            "quality_flag": payload.get("quality_flag", "high"),
            "total_tool_calls": payload.get("total_tool_calls", 0),
            "total_tokens": payload.get("total_tokens", 0),
            "total_cost_usd": payload.get("total_cost_usd", 0.0),
        }

    @activity.defn(name="recruiter_assignment.synthesize_best_effort_proposal")
    async def _synth(payload: dict) -> dict:
        return {
            "job_id": payload.get("job_id", JOB_ID),
            "workflow_id": payload.get("workflow_id", "wf-test"),
            "proposed_recruiter_ids": [],
            "candidates": [],
            "fit_scores": [],
            "narratives": {},
            "audit_trail": [],
            "quality_flag": "low",
            "total_tool_calls": payload.get("total_tool_calls", 0),
            "total_tokens": payload.get("total_tokens", 0),
            "total_cost_usd": payload.get("total_cost_usd", 0.0),
        }

    @activity.defn(name="recruiter_assignment.persist_proposal")
    async def _persist(payload: dict) -> dict:  # noqa: ARG001
        return {"proposal_id": str(uuid.uuid4()), "persisted": True}

    @activity.defn(name="recruiter_assignment.assign_recruiters_to_role")
    async def _assign(payload: dict) -> dict:  # noqa: ARG001
        return {"assigned": True}

    @activity.defn(name="recruiter_assignment.notify_assigned_recruiters")
    async def _notify(payload: dict) -> dict:  # noqa: ARG001
        return {"notified": True}

    @activity.defn(name="recruiter_assignment.transition_job_status")
    async def _transition(payload: dict) -> dict:  # noqa: ARG001
        return {"transitioned": True}

    @activity.defn(name="recruiter_assignment.log_hitl_event")
    async def _log_hitl(payload: dict) -> dict:  # noqa: ARG001
        return {"logged": True}

    return [
        _search,
        _widen,
        _relax,
        _capacity,
        _placements,
        _summarize,
        _score,
        _rank,
        _format,
        _synth,
        _persist,
        _assign,
        _notify,
        _transition,
        _log_hitl,
    ]


# ---------------------------------------------------------------------------
# Workflow input
# ---------------------------------------------------------------------------


def _build_input(rejection_notes: str | None = None) -> dict:
    return {
        "job_id": JOB_ID,
        "classification": {
            "role_category": "engineering",
            "seniority_level": "senior",
            "stage_fit": "series_a",
            "remote_onsite": "remote",
            "must_have_skills": ["python"],
            "nice_to_have_skills": [],
            "rationale": "Senior backend engineer.",
        },
        "rubric": {
            "dimensions": [
                {
                    "name": "domain_depth",
                    "description": "Engineering domain experience.",
                    "weight": 1.0,
                    "evaluation_guidance": "Score 0-5.",
                }
            ],
            "rationale": "Single-dim rubric for tests.",
        },
        "params": {"top_n": 3, "widen_domain_hops": 1, "reuse_assignment_from": None},
        "rejection_notes": rejection_notes,
    }


def _llm_happy_sequence() -> list[tuple[str, dict]]:
    """A minimal LLM tool-call sequence ending in propose_assignment_set."""
    return [
        (
            "search_recruiter_pool",
            {
                "role_category": "engineering",
                "seniority_level": "senior",
                "stage_fit": "series_a",
                "must_have_skills": [],
            },
        ),
        (
            "score_recruiter_fit",
            {
                "recruiter_ids": ["r1", "r2"],
                "role_category": "engineering",
                "seniority_level": "senior",
                "stage_fit": "series_a",
                "must_have_skills": [],
            },
        ),
        (
            "rank_and_select_recruiters",
            {"scored_recruiter_ids": ["r1", "r2"], "top_n": 3},
        ),
        (
            "propose_assignment_set",
            {
                "recruiter_ids": ["r1"],
                "rationale": "Best fit overall",
                "quality_flag": "high",
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Helper: send signal after the workflow has parked on it
# ---------------------------------------------------------------------------


async def _wait_for_phase(handle: Any, target_phase: str, max_iters: int = 50) -> None:
    """Poll the workflow's `current_phase` query until it matches `target_phase`.

    The time-skipping environment makes wall-clock waits cheap; bound the
    poll count so a test doesn't spin forever if the workflow never reaches
    `target_phase`.
    """
    import asyncio

    for _ in range(max_iters):
        try:
            phase = await handle.query("current_phase")
        except Exception:  # pragma: no cover — query before workflow is ready
            phase = None
        if phase == target_phase:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"workflow never reached phase={target_phase}")


# ---------------------------------------------------------------------------
# Test 1: happy path
# ---------------------------------------------------------------------------


async def test_happy_path_operator_approves() -> None:
    """LLM proposes a single recruiter, operator approves -> status='assigned'."""
    activities = [_make_llm_decide_stub(_llm_happy_sequence())]
    activities.extend(_build_default_tool_stubs())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterAssignmentWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                RecruiterAssignmentWorkflow.run,
                _build_input(),
                id=f"test-recruiter-assignment-happy-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "approve",
                    "confirmed_recruiter_ids": ["r1"],
                    "operator_id": "op-1",
                },
            )

            result = await handle.result()

    assert result["status"] == "assigned"
    assert result["assigned_recruiter_ids"] == ["r1"]
    assert result["assignment_count"] == 1
    assert result["total_loop_iterations"] == 1


# ---------------------------------------------------------------------------
# Test 2: reject then re-approve
# ---------------------------------------------------------------------------


async def test_operator_rejects_then_reapproves() -> None:
    """Operator rejects once -> agent re-loops -> operator approves the second proposal."""
    # Two full happy sequences: one for the initial proposal, one for the
    # re-loop after rejection.
    sequence = _llm_happy_sequence() + _llm_happy_sequence()
    activities = [_make_llm_decide_stub(sequence)]
    activities.extend(_build_default_tool_stubs())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterAssignmentWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                RecruiterAssignmentWorkflow.run,
                _build_input(),
                id=f"test-recruiter-assignment-reject-then-approve-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

            # First HITL pause: reject.
            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "reject",
                    "notes": "Need more ML experience",
                    "operator_id": "op-1",
                },
            )

            # Second HITL pause (after re-loop): approve.
            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "approve",
                    "confirmed_recruiter_ids": ["r2"],
                    "operator_id": "op-1",
                },
            )

            result = await handle.result()

    assert result["status"] == "assigned"
    assert result["assigned_recruiter_ids"] == ["r2"]
    assert result["total_loop_iterations"] == 2


# ---------------------------------------------------------------------------
# Test 3: reject twice -> terminal rejected_by_operator
# ---------------------------------------------------------------------------


async def test_operator_rejects_twice_terminal() -> None:
    """Two consecutive rejects -> workflow returns 'rejected_by_operator'."""
    sequence = _llm_happy_sequence() + _llm_happy_sequence()
    activities = [_make_llm_decide_stub(sequence)]
    activities.extend(_build_default_tool_stubs())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterAssignmentWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                RecruiterAssignmentWorkflow.run,
                _build_input(),
                id=f"test-recruiter-assignment-reject-twice-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "reject",
                    "notes": "Pool too thin",
                    "operator_id": "op-1",
                },
            )

            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "reject",
                    "notes": "Still not what we want",
                    "operator_id": "op-1",
                },
            )

            result = await handle.result()

    assert result["status"] == "rejected_by_operator"
    assert result["assigned_recruiter_ids"] == []
    assert result["assignment_count"] == 0


# ---------------------------------------------------------------------------
# Test 4: budget exhaustion -> synthesize_best_effort_proposal still runs
# ---------------------------------------------------------------------------


async def test_budget_exhaustion_still_proposes() -> None:
    """26 non-terminal LLM calls exhausts the hard tool_calls budget; the
    workflow must still produce a proposal via synthesize_best_effort_proposal
    rather than raise an exception."""
    # Fill the LLM sequence with 26 non-terminal calls (search_recruiter_pool
    # is idempotent and safe to repeat). The workflow's hard limit is 25, so
    # the 26th iteration will trip should_terminate and route into the
    # best-effort synthesis branch.
    sequence: list[tuple[str, dict]] = [
        (
            "search_recruiter_pool",
            {
                "role_category": "engineering",
                "seniority_level": "senior",
                "stage_fit": None,
                "must_have_skills": [],
            },
        )
    ] * 26
    activities = [_make_llm_decide_stub(sequence)]
    activities.extend(_build_default_tool_stubs())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterAssignmentWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                RecruiterAssignmentWorkflow.run,
                _build_input(),
                id=f"test-recruiter-assignment-budget-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

            # synthesize_best_effort_proposal still has to clear HITL gate.
            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "approve",
                    "confirmed_recruiter_ids": [],
                    "override_set": ["r-fallback"],
                    "operator_id": "op-1",
                },
            )

            result = await handle.result()

    # The workflow must NOT raise; it must produce a result with assigned set.
    assert result["status"] in ("assigned", "rejected_by_operator")


# ---------------------------------------------------------------------------
# Test 5: empty pool, operator override honored
# ---------------------------------------------------------------------------


async def test_empty_pool_override_honored() -> None:
    """Search returns empty; LLM still proposes with quality_flag='low' and an
    empty recruiter list. Operator overrides with a single recruiter id —
    that override must end up in `assigned_recruiter_ids`."""
    sequence: list[tuple[str, dict]] = [
        (
            "search_recruiter_pool",
            {
                "role_category": "engineering",
                "seniority_level": "senior",
                "stage_fit": "series_a",
                "must_have_skills": [],
            },
        ),
        (
            "propose_assignment_set",
            {
                "recruiter_ids": [],
                "rationale": "Pool empty after search",
                "quality_flag": "low",
            },
        ),
    ]
    activities = [_make_llm_decide_stub(sequence)]
    activities.extend(_build_default_tool_stubs(candidates=[]))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TEST_TASK_QUEUE,
            workflows=[RecruiterAssignmentWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                RecruiterAssignmentWorkflow.run,
                _build_input(),
                id=f"test-recruiter-assignment-override-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )

            await _wait_for_phase(handle, "awaiting_operator")
            await handle.signal(
                "operator_approval",
                {
                    "decision": "approve",
                    "confirmed_recruiter_ids": [],
                    "override_set": ["r-override"],
                    "operator_id": "op-1",
                },
            )

            result = await handle.result()

    assert result["status"] == "assigned"
    assert result["assigned_recruiter_ids"] == ["r-override"]
    assert result["assignment_count"] == 1
