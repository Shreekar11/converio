"""Tests for augmented LLM primitive: Budget, ToolRegistry, PromptAssembly.

These are pure-Python W0 foundation modules — no Temporal worker, no DB,
no LLM. The tests assert the externally-visible contracts that the
Recruiter Assignment workflow relies on:

  - Budget: charge accumulation, soft-warning vs hard-terminate
    semantics, serialize() shape, exhausted_reason() priority.
  - ToolRegistry: get_tool() lookup behaviour, RECRUITER_ASSIGNMENT_TOOLS
    catalog completeness, render_tools_for_llm() output shape.
  - PromptAssembly: render_history() compaction rules, append_history()
    immutability, build_llm_decision_payload() shape, GOALS coverage.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.temporal.core.budget import Budget, BudgetExhaustedReason
from app.temporal.core.prompt_assembly import (
    GOALS,
    HistoryEntry,
    append_history,
    build_llm_decision_payload,
    render_history,
)
from app.temporal.core.tool_registry import (
    RECRUITER_ASSIGNMENT_TOOLS,
    ToolSpec,
    get_tool,
    render_tools_for_llm,
)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_default_instantiation() -> None:
    """Default Budget exposes the soft/hard limits documented in the spec."""
    b = Budget()
    assert b.soft_tool_calls == 15
    assert b.hard_tool_calls == 25
    assert b.soft_tokens == 30_000
    assert b.hard_tokens == 60_000
    assert b.soft_cost_usd == 0.50
    assert b.hard_cost_usd == 1.00
    assert b.soft_wallclock_seconds == 120.0
    assert b.hard_wallclock_seconds == 300.0


def test_budget_charge_accumulates() -> None:
    """charge() increments the internal counters; should_terminate stays False
    while we're well under all hard limits."""
    b = Budget()
    for _ in range(3):
        b.charge(tokens=100, cost_usd=0.01)

    serialized = b.serialize()
    assert serialized["tool_calls_used"] == 3
    assert serialized["tokens_used"] == 300
    assert serialized["cost_usd_used"] == pytest.approx(0.03, abs=1e-6)
    assert serialized["status"] == "ok"
    assert b.should_terminate() is False


def test_budget_soft_warning() -> None:
    """At soft_tool_calls charges, is_soft_warning() is True but should_terminate
    remains False."""
    b = Budget()
    for _ in range(b.soft_tool_calls):
        b.charge()

    assert b.is_soft_warning() is True
    assert b.should_terminate() is False


def test_budget_hard_terminate() -> None:
    """At hard_tool_calls charges, should_terminate() flips to True."""
    b = Budget()
    for _ in range(b.hard_tool_calls):
        b.charge()

    assert b.should_terminate() is True
    # Once terminated, is_soft_warning() returns False (terminate strictly
    # supersedes the soft-warning state).
    assert b.is_soft_warning() is False


def test_budget_serialize_shape() -> None:
    """serialize() returns the keys the workflow + LLM prompt rely on."""
    b = Budget()
    b.charge(tokens=500, cost_usd=0.05)
    serialized = b.serialize()

    required_keys = {
        "tool_calls_used",
        "tool_calls_remaining",
        "tokens_used",
        "tokens_remaining",
        "cost_usd_used",
        "cost_usd_remaining",
        "elapsed_seconds",
        "wallclock_remaining",
        "status",
    }
    assert required_keys.issubset(serialized.keys())
    assert serialized["status"] == "ok"


def test_budget_exhausted_reason_priority() -> None:
    """When tool_calls is exhausted first, exhausted_reason() returns TOOL_CALLS.

    Priority order in the impl is: tool_calls -> tokens -> cost -> wallclock.
    """
    b = Budget()
    for _ in range(b.hard_tool_calls):
        b.charge()
    assert b.exhausted_reason() == BudgetExhaustedReason.TOOL_CALLS


def test_budget_exhausted_reason_tokens() -> None:
    """If only tokens cross the hard limit, exhausted_reason() reports TOKENS."""
    b = Budget()
    # One charge with enough tokens to bust the token ceiling without
    # touching tool_calls (tool_calls increments by 1 only).
    b.charge(tokens=b.hard_tokens + 1)
    assert b.exhausted_reason() == BudgetExhaustedReason.TOKENS
    assert b.should_terminate() is True


def test_budget_exhausted_reason_wallclock() -> None:
    """Wallclock exhaustion is detected via elapsed_seconds vs hard_wallclock."""
    b = Budget()
    # Simulate elapsed time by reaching past the hard wallclock limit.
    fake_now = b._started_at + b.hard_wallclock_seconds + 1.0
    with patch("app.temporal.core.budget.time.monotonic", return_value=fake_now):
        assert b.exhausted_reason() == BudgetExhaustedReason.WALLCLOCK
        assert b.should_terminate() is True


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_get_tool_known() -> None:
    spec = get_tool("search_recruiter_pool")
    assert isinstance(spec, ToolSpec)
    assert spec.name == "search_recruiter_pool"
    assert spec.activity_name == "recruiter_assignment.search_recruiter_pool"


def test_get_tool_unknown() -> None:
    with pytest.raises(KeyError):
        get_tool("nonexistent_tool")


def test_recruiter_assignment_tools_count() -> None:
    """The Recruiter Assignment Agent exposes exactly 10 tools (per spec)."""
    assert len(RECRUITER_ASSIGNMENT_TOOLS) == 10


def test_render_tools_for_llm_nonempty() -> None:
    rendered = render_tools_for_llm(RECRUITER_ASSIGNMENT_TOOLS)
    assert isinstance(rendered, str)
    assert len(rendered) > 0
    assert "search_recruiter_pool" in rendered
    # JSON Schema fences exist so the LLM can parse arg shapes.
    assert "```json" in rendered


def test_all_tools_have_required_fields() -> None:
    """Each ToolSpec must declare the fields the runtime/LLM rely on."""
    for spec in RECRUITER_ASSIGNMENT_TOOLS:
        assert spec.name, "ToolSpec missing name"
        assert spec.activity_name, f"ToolSpec '{spec.name}' missing activity_name"
        assert spec.description_md, f"ToolSpec '{spec.name}' missing description_md"
        assert spec.when_to_use_md, f"ToolSpec '{spec.name}' missing when_to_use_md"
        assert spec.when_not_to_use_md, (
            f"ToolSpec '{spec.name}' missing when_not_to_use_md"
        )
        assert isinstance(spec.arg_schema, dict)
        assert spec.return_description, (
            f"ToolSpec '{spec.name}' missing return_description"
        )


# ---------------------------------------------------------------------------
# PromptAssembly
# ---------------------------------------------------------------------------


def _entry(step: int) -> HistoryEntry:
    return HistoryEntry(
        step=step,
        tool_name=f"tool_{step}",
        args={"k": f"v{step}"},
        result_summary=f"result for step {step}",
        tokens_used=10,
        cost_usd=0.001,
        latency_ms=15,
    )


def test_render_history_empty() -> None:
    assert render_history([]) == "(no tool calls yet)"


def test_render_history_compaction() -> None:
    """With 12 entries and verbatim_last_n=8, the first 4 are compacted."""
    entries = [_entry(i) for i in range(12)]
    rendered = render_history(entries, verbatim_last_n=8)

    assert "## Earlier calls (compacted)" in rendered
    assert "## Recent calls (verbatim)" in rendered

    # First 4 (steps 0-3) appear in the compacted section.
    for i in range(4):
        assert f"Step {i}: tool_{i}" in rendered

    # Last 8 (steps 4-11) appear in the verbatim section with full args.
    for i in range(4, 12):
        assert f"Step {i} — tool_{i}" in rendered


def test_render_history_no_compaction_needed() -> None:
    """If history length <= verbatim_last_n, only the verbatim section appears."""
    entries = [_entry(i) for i in range(3)]
    rendered = render_history(entries, verbatim_last_n=8)

    assert "## Earlier calls (compacted)" not in rendered
    assert "## Recent calls (verbatim)" in rendered


def test_append_history_immutable() -> None:
    """append_history returns a NEW list; the input list is not mutated."""
    original: list[HistoryEntry] = []
    new_history = append_history(
        original,
        step=0,
        tool_name="foo",
        args={"a": 1},
        result_summary="done",
    )

    assert original == []
    assert len(new_history) == 1
    assert new_history[0].tool_name == "foo"
    assert new_history is not original


def test_build_llm_decision_payload_shape() -> None:
    """build_llm_decision_payload returns the 5 keys the LLM activity expects."""
    payload = build_llm_decision_payload(
        agent_key="recruiter_assignment",
        tool_catalog_md="<catalog>",
        history=[],
        budget_dict={"tool_calls_used": 0},
        context={"job_id": "j1"},
    )
    expected_keys = {
        "goal_text",
        "tool_catalog_md",
        "history_md",
        "budget_json",
        "context_json",
    }
    assert set(payload.keys()) == expected_keys
    assert "(no tool calls yet)" in payload["history_md"]


def test_goals_recruiter_assignment_exists() -> None:
    """The recruiter_assignment agent goal text must be defined and non-empty."""
    assert "recruiter_assignment" in GOALS
    goal = GOALS["recruiter_assignment"]
    assert isinstance(goal, str)
    assert len(goal) > 0
    # Sanity-check the goal text references the terminal tool.
    assert "propose_assignment_set" in goal


def test_unknown_agent_key_raises() -> None:
    with pytest.raises(ValueError, match="Unknown agent_key"):
        build_llm_decision_payload(
            agent_key="not_a_real_agent",
            tool_catalog_md="",
            history=[],
            budget_dict={},
            context={},
        )
