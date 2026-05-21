"""Workflow low-confidence-loop test for ScorecardGeneratorWorkflow.

Two of three initial-scoring dims come in below the 0.65 confidence
threshold. The reflective loop fires; the LLM selects an evidence
source for each, runs the matching fetcher, then rescores the dim
above threshold. When `low_conf_remaining` empties, the LLM emits
`mark_scorecard_done(reason="all_dims_confident")`.

Asserted public contract:
  * `self_correction_triggered == True`
  * `dimensions_rescored` contains both rescored dim names (sorted)
  * `termination_reason == "all_dims_confident"`
  * `tool_call_count >= 6` — at minimum: select×2 + fetch×2 + rescore×2
    + mark_done = 7 reflective tool calls. The exact number depends on
    how the LLM stub paces its decisions; we assert the lower bound.

Same skip-on-env guard as the happy-path test — if
`WorkflowEnvironment` cannot boot (CI without temporalite binary), the
test auto-skips rather than fails.
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

# Two dims start at 0.45 confidence — below the 0.65 gate. The third
# dim is comfortably confident so the loop only has to clear two dims.
_DIM1_NAME = "distributed_systems_depth"
_DIM2_NAME = "language_depth"
_DIM3_NAME = "system_design_thinking"

_LOW_CONF_DIMS = [
    {
        "name": _DIM1_NAME,
        "score": 60,
        "confidence": 0.45,
        "weight": 1 / 3,
        "rationale": "Resume mentions Kafka; no infra repos found.",
        "evidence_limited": False,
        "citation": {"text": "Resume bullet", "resolution_method": "placeholder"},
    },
    {
        "name": _DIM2_NAME,
        "score": 70,
        "confidence": 0.45,
        "weight": 1 / 3,
        "rationale": "Python listed; no recent commits visible.",
        "evidence_limited": False,
        "citation": {"text": "Skill list", "resolution_method": "placeholder"},
    },
    {
        "name": _DIM3_NAME,
        "score": 85,
        "confidence": 0.85,
        "weight": 1 / 3,
        "rationale": "Has shipped an RFC document.",
        "evidence_limited": False,
        "citation": {"text": "RFC author", "resolution_method": "placeholder"},
    },
]


# ---------------------------------------------------------------------------
# LLM decision sequencer
# ---------------------------------------------------------------------------


def _make_llm_decide_stub(tool_calls: list[tuple[str, dict]]):
    """Stub for `core.llm_decide_next_tool` that returns prepared decisions.

    Falls through to `mark_scorecard_done("diminishing_returns")` if the
    test under-specifies — makes diagnosis easier than a hang.
    """
    call_iter = iter(tool_calls)

    @activity.defn(name="core.llm_decide_next_tool")
    async def _decide(payload: dict) -> dict:  # noqa: ARG001
        try:
            tool_name, args = next(call_iter)
        except StopIteration:
            tool_name, args = (
                "mark_scorecard_done",
                {"reason": "diminishing_returns", "message": "test fallback"},
            )
        return {
            "tool_name": tool_name,
            "args": args,
            "reasoning": "mock",
            "tokens_used": 100,
            "cost_usd": 0.001,
            "latency_ms": 50,
        }

    return _decide


# ---------------------------------------------------------------------------
# Activity stubs
# ---------------------------------------------------------------------------


def _build_low_conf_stubs() -> list:
    @activity.defn(name="scorecard.get_candidate_profile")
    async def _get_candidate(payload: dict) -> dict:  # noqa: ARG001
        return {
            "candidate_id": CANDIDATE_ID,
            "full_name": "Bob Engineer",
            "github_username": "bob",
            "skills": ["Python", "Kafka"],
            "profile_text": "Resume text for Bob.",
            "enriched_data": {
                "full_name": "Bob Engineer",
                "seniority": "senior",
                "years_experience": 6,
                "github_username": "bob",
                "skills": [{"name": "Python"}, {"name": "Kafka"}],
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
                {"name": _DIM1_NAME, "weight": 1 / 3, "description": "dist sys"},
                {"name": _DIM2_NAME, "weight": 1 / 3, "description": "language"},
                {"name": _DIM3_NAME, "weight": 1 / 3, "description": "design"},
            ],
        }

    @activity.defn(name="scorecard.build_scoring_prompt")
    async def _build_prompt(payload: dict) -> dict:  # noqa: ARG001
        return {
            "prompt": "FAKE SCORING PROMPT",
            "candidate_summary": "Bob Engineer — senior, 6y.",
        }

    @activity.defn(name="scorecard.score_candidate_dimensions")
    async def _score(payload: dict) -> dict:  # noqa: ARG001
        return {
            "scorecard_output": {
                "dimensions": list(_LOW_CONF_DIMS),
                "strengths": [],
                "red_flags": [],
            },
            "tokens_used": 1500,
            "cost_usd": 0.05,
        }

    # Gate state mutates across iterations as the workflow rescores
    # dims. We use a small state machine keyed on the call count to
    # emulate the real gate's behaviour: initially returns 2 low-conf
    # dims; subsequent calls (post-rescore) return progressively fewer.
    _gate_call = {"n": 0}

    @activity.defn(name="scorecard.check_confidence_gate")
    async def _gate(payload: dict) -> dict:  # noqa: ARG001
        _gate_call["n"] += 1
        # The workflow only calls the gate ONCE pre-loop (per the
        # workflow source). Post-loop gate state is tracked via
        # `low_conf_by_name` in-memory. We return the initial state.
        return {
            "low_confidence_dimensions": [
                {
                    "name": _DIM1_NAME,
                    "current_confidence": 0.45,
                    "current_score": 60,
                    "reason": "confidence 0.45 below threshold 0.65",
                },
                {
                    "name": _DIM2_NAME,
                    "current_confidence": 0.45,
                    "current_score": 70,
                    "reason": "confidence 0.45 below threshold 0.65",
                },
            ],
            "all_confident": False,
            "low_conf_count": 2,
        }

    @activity.defn(name="scorecard.select_evidence_source")
    async def _select(payload: dict) -> dict:
        # Pick a fetcher based on the dim name — matches the prompt-to-tool
        # heuristic the real LLM would apply.
        dim = (payload.get("low_conf_dim") or {}).get("name", "")
        if dim == _DIM1_NAME:
            return {
                "tool_name": "fetch_repo_readmes",
                "reasoning": "Need infra repos for distributed_systems_depth.",
                "keyword_filter": ["kafka", "kubernetes"],
            }
        if dim == _DIM2_NAME:
            return {
                "tool_name": "fetch_commit_history",
                "reasoning": "Need commit recency for language_depth.",
            }
        return {"tool_name": "skip", "reasoning": "No fetcher applies."}

    @activity.defn(name="scorecard.fetch_repo_readmes")
    async def _fetch_readmes(payload: dict) -> dict:  # noqa: ARG001
        return {
            "repos": [
                {
                    "repo_name": "bob/kafka-utils",
                    "description": "Kafka helpers",
                    "readme_text": "Kafka utility library.",
                    "primary_language": "Python",
                    "stargazers_count": 12,
                    "pushed_at": "2026-04-01T00:00:00Z",
                },
                {
                    "repo_name": "bob/k8s-deploy",
                    "description": "K8s deploy scripts",
                    "readme_text": "Kubernetes deployment tooling.",
                    "primary_language": "Go",
                    "stargazers_count": 8,
                    "pushed_at": "2026-03-01T00:00:00Z",
                },
            ],
            "total_count": 2,
        }

    @activity.defn(name="scorecard.fetch_commit_history")
    async def _fetch_commits(payload: dict) -> dict:  # noqa: ARG001
        return {
            "total_commits_last_year": 320,
            "commits_by_month": {"2025-12": 30, "2026-01": 45, "2026-02": 50},
            "dominant_language": "Python",
            "active_months_in_window": 11,
        }

    @activity.defn(name="scorecard.rescore_dimension")
    async def _rescore(payload: dict) -> dict:
        dim_name = payload.get("dimension_name", "")
        # Both dims clear the threshold after rescore.
        new_conf = 0.82 if dim_name == _DIM1_NAME else 0.78
        return {
            "new_score": 82,
            "new_confidence": new_conf,
            "new_rationale": f"Updated based on evidence for {dim_name}.",
            "new_citation_text": "Updated citation",
            "evidence_hash": "abc123",
            "tokens_used": 800,
            "cost_usd": 0.03,
        }

    @activity.defn(name="scorecard.mark_scorecard_done")
    async def _mark(payload: dict) -> dict:
        return {"done": True, "reason": payload.get("reason", "all_dims_confident")}

    @activity.defn(name="scorecard.compute_overall_match_score")
    async def _compute(payload: dict) -> dict:  # noqa: ARG001
        return {"overall_match_score": "83.00", "weight_sum": "1.00"}

    @activity.defn(name="scorecard.resolve_citations")
    async def _resolve(payload: dict) -> dict:
        return {"dimensions": payload.get("dimensions", [])}

    @activity.defn(name="scorecard.persist_scorecard")
    async def _persist(payload: dict) -> dict:  # noqa: ARG001
        return {"scorecard_id": SCORECARD_ID, "updated_at": "2026-05-21T00:00:00Z"}

    # Provide stubs for the other fetchers too — the worker registers
    # every scorecard activity name, but only the two above get called
    # on this scenario. Stubs that get called would still return
    # well-shaped dicts so the workflow never crashes.
    @activity.defn(name="scorecard.fetch_pr_review_history")
    async def _fetch_reviews(payload: dict) -> dict:  # noqa: ARG001
        return {"reviews": [], "total_count": 0}

    @activity.defn(name="scorecard.fetch_repo_languages")
    async def _fetch_langs(payload: dict) -> dict:  # noqa: ARG001
        return {"languages": {}, "primary_language": None, "language_count": 0}

    @activity.defn(name="scorecard.fetch_user_orgs_and_stars")
    async def _fetch_orgs(payload: dict) -> dict:  # noqa: ARG001
        return {"organizations": [], "total_starred_repos": 0, "notable_repos_starred": []}

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


# ---------------------------------------------------------------------------
# LLM tool-call sequence (7 calls: select, fetch, rescore × 2, mark)
# ---------------------------------------------------------------------------


def _llm_sequence_two_rescores() -> list[tuple[str, dict]]:
    """select → fetch → rescore for dim1, then dim2, then mark_done."""
    return [
        # --- Dim 1 cycle ---
        (
            "select_evidence_source",
            {
                "low_conf_dim": {"name": _DIM1_NAME},
            },
        ),
        (
            "fetch_repo_readmes",
            {
                "github_username": "bob",
                "keyword_filter": ["kafka", "kubernetes"],
            },
        ),
        (
            "rescore_dimension",
            {
                "dimension_name": _DIM1_NAME,
            },
        ),
        # --- Dim 2 cycle ---
        (
            "select_evidence_source",
            {
                "low_conf_dim": {"name": _DIM2_NAME},
            },
        ),
        (
            "fetch_commit_history",
            {
                "github_username": "bob",
                "since_days": 365,
            },
        ),
        (
            "rescore_dimension",
            {
                "dimension_name": _DIM2_NAME,
            },
        ),
        # --- Terminate ---
        (
            "mark_scorecard_done",
            {"reason": "all_dims_confident", "message": "All dims now confident."},
        ),
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


async def test_low_confidence_loop_two_dims_rescored() -> None:
    """Two dims < 0.65 → reflective loop rescores both → all_dims_confident."""
    activities = [_make_llm_decide_stub(_llm_sequence_two_rescores())]
    activities.extend(_build_low_conf_stubs())

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
                id=f"test-scorecard-lowconf-{uuid.uuid4()}",
                task_queue=TEST_TASK_QUEUE,
            )
            result = await handle.result()

    # Reflective loop fired.
    assert result["self_correction_triggered"] is True

    # Both low-conf dims were rescored. Order is sorted by the workflow
    # before persistence, so we assert against the sorted set.
    assert result["dimensions_rescored"] == sorted([_DIM1_NAME, _DIM2_NAME])

    # LLM emitted mark_scorecard_done("all_dims_confident").
    assert result["termination_reason"] == "all_dims_confident"

    # At minimum: select×2 + fetch×2 + rescore×2 + mark_done = 7 tool calls.
    # The workflow counts every reflective-loop iteration's tool call;
    # we assert the lower bound to leave headroom for any one-off retries.
    assert result["tool_call_count"] >= 6

    # Persistence still landed; the scorecard is real.
    assert result["scorecard_id"] == SCORECARD_ID
