"""Core tool dispatcher activity for agent loops.

`core.execute_tool` receives a tool_name + args dict, validates args against
the ToolSpec's arg_schema (basic key presence check), dispatches to the
appropriate Temporal activity via `workflow.execute_activity` calls from
within the workflow, OR directly invokes the activity function if called
standalone.

IMPORTANT DESIGN NOTE: This dispatcher is called from WORKFLOW CODE (not from
within another activity) because it needs to call `workflow.execute_activity`.
It is registered as an activity for observability/history, but it is the
WORKFLOW's responsibility to call each tool's backing activity directly --
not this dispatcher.

For PoW simplicity: the dispatcher is implemented as a pure-Python helper
(not a Temporal activity wrapping activity calls, which is not allowed).
The workflow calls this helper synchronously to get the activity name + validated
args, then calls `workflow.execute_activity(activity_name, args)` directly.
"""

from __future__ import annotations

from datetime import timedelta

from app.temporal.core.tool_registry import get_tool

__all__ = [
    "resolve_tool_call",
    "TOOL_ACTIVITY_TIMEOUTS",
    "get_tool_timeout",
    "is_terminal_tool",
]


def resolve_tool_call(tool_name: str, args: dict) -> tuple[str, dict]:
    """Validate a tool call and return (activity_name, validated_args).

    Args:
        tool_name: must be registered in RECRUITER_ASSIGNMENT_TOOLS
        args: must contain all required keys from ToolSpec.arg_schema

    Returns:
        (activity_name, args) ready to pass to workflow.execute_activity

    Raises:
        KeyError: unknown tool_name
        ValueError: missing required arg keys
    """
    spec = get_tool(tool_name)  # raises KeyError if unknown

    # Basic required-key validation from arg_schema. We intersect declared
    # `properties` with `required` so a malformed schema (required key without
    # a properties entry) does not silently mask a missing arg; it still ends
    # up in the required set if the schema lists it.
    properties = spec.arg_schema.get("properties", {}) or {}
    required_list = spec.arg_schema.get("required", []) or []
    required_keys = {k for k in required_list if k in properties}

    missing = required_keys - args.keys()
    if missing:
        raise ValueError(
            f"Tool {tool_name!r} missing required args: {missing}"
        )

    return spec.activity_name, args


# ---------------------------------------------------------------------------
# Per-activity Temporal schedule_to_close timeouts
# ---------------------------------------------------------------------------
# Tuned per tool cost class (see tool_registry.CostClass):
#   - FREE / Cypher / deterministic compute  -> 5-30s
#   - SMALL  (per-recruiter LLM polish)      -> 60s
#   - MEDIUM (batched LLM scoring)           -> 60s
# Capacity / placement lookups are tight (15s) because they are simple
# Postgres reads on indexed columns; if they exceed that we want fast failure
# so the agent loop can decide whether to retry or fall back.

TOOL_ACTIVITY_TIMEOUTS: dict[str, timedelta] = {
    # Recruiter Assignment Agent (Agent 0)
    "recruiter_assignment.search_recruiter_pool": timedelta(seconds=30),
    "recruiter_assignment.widen_domain_search": timedelta(seconds=30),
    "recruiter_assignment.relax_stage_match": timedelta(seconds=30),
    "recruiter_assignment.query_recruiter_capacity": timedelta(seconds=15),
    "recruiter_assignment.check_recent_placements": timedelta(seconds=15),
    "recruiter_assignment.summarize_recruiter_track_record": timedelta(seconds=60),
    "recruiter_assignment.score_recruiter_fit": timedelta(seconds=60),
    "recruiter_assignment.rank_and_select_recruiters": timedelta(seconds=5),
    "recruiter_assignment.format_recruiter_recommendations": timedelta(seconds=5),
    "recruiter_assignment.propose_assignment_set": timedelta(seconds=5),
    # Scorecard Generator Agent (Agent 4) — MVP (GitHub-only) tools.
    # GitHub fetchers get 30s because the GitHub REST API p95 is well under
    # a second per request, but each fetcher may issue 5-10 requests with
    # retries on transient 5xx. select_evidence_source is a small (Flash)
    # structured call; rescore_dimension is a larger (Pro) structured call.
    # mark_scorecard_done is a terminal control flip and needs almost no
    # time at all.
    "scorecard.fetch_repo_readmes": timedelta(seconds=30),
    "scorecard.fetch_commit_history": timedelta(seconds=30),
    "scorecard.fetch_pr_review_history": timedelta(seconds=30),
    "scorecard.fetch_repo_languages": timedelta(seconds=30),
    "scorecard.fetch_user_orgs_and_stars": timedelta(seconds=30),
    "scorecard.select_evidence_source": timedelta(seconds=60),
    "scorecard.rescore_dimension": timedelta(seconds=90),
    "scorecard.mark_scorecard_done": timedelta(seconds=5),
}

_DEFAULT_TIMEOUT = timedelta(seconds=30)


def get_tool_timeout(activity_name: str) -> timedelta:
    """Return the schedule_to_close_timeout for `activity_name`.

    Falls back to a conservative 30s default for any activity not explicitly
    listed. Unknown activity names are not an error here -- the workflow will
    surface them via Temporal's own activity-not-registered error.
    """
    return TOOL_ACTIVITY_TIMEOUTS.get(activity_name, _DEFAULT_TIMEOUT)


# ---------------------------------------------------------------------------
# Terminal tools
# ---------------------------------------------------------------------------
# `propose_assignment_set` ends the agent's tool-calling loop: once the LLM
# decides to propose, the workflow stops calling `llm_decide_next_tool` and
# moves on to operator HITL review. The workflow checks `is_terminal_tool`
# after each LLM decision to know whether to break the loop.

_TERMINAL_TOOLS: frozenset[str] = frozenset(
    {
        # Recruiter Assignment Agent terminal tool.
        "propose_assignment_set",
        # Scorecard Generator Agent terminal tool. When the LLM emits
        # `mark_scorecard_done` the workflow stops calling
        # select_evidence_source / rescore_dimension and advances to the
        # deterministic compute_overall_match_score → resolve_citations →
        # persist_scorecard tail.
        "mark_scorecard_done",
    }
)


def is_terminal_tool(tool_name: str) -> bool:
    """Return True iff `tool_name` is a loop-terminating tool.

    Compares against the tool *name* (not activity_name), matching what the
    LLM emits in its ToolCallDecision.tool_name field.
    """
    return tool_name in _TERMINAL_TOOLS
