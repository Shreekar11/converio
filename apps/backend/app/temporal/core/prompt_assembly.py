"""Prompt assembly for agent tool-calling loops.

Builds the per-iteration prompt that the LLM sees: goal text, tool catalog,
call history (with compaction for older entries), budget, and role context.

History compaction: the last N entries are shown verbatim; older entries are
summarized to a single line each to stay within the token budget.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_RESULT_SUMMARY_MAX_CHARS = 500


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    tool_name: str
    args: dict[str, Any]
    result_summary: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


GOALS: dict[str, str] = {
    "scorecard": """
You are the Scorecard Generator Agent for Contrario Match.

MISSION
Produce a complete, evidence-backed scorecard for a single candidate against
the role rubric. You will be given the candidate profile (already enriched by
upstream indexing, including GitHub signals if available), the role rubric,
and an initial scorecard produced by a structured-output LLM call. Some
dimensions in that initial scorecard will have confidence < 0.65 — your job
is to selectively gather more evidence and rescore only those dimensions.

WHAT "GOOD ENOUGH" LOOKS LIKE
- Every rubric dimension has either confidence >= 0.65, OR a documented
  reason for remaining low-confidence (e.g. evidence-source unavailable for
  this candidate type).
- Each rescored dimension cites concrete evidence pulled by a fetcher tool.
- The overall_match_score is computed by the deterministic weighted average
  activity, not by you — do not output that field.
- Citations refer to real text in the candidate profile; the citation
  resolver runs after you finish.

HARD CONSTRAINTS
- You may call at most 8 tools; total wallclock 180 seconds; LLM cost
  ceiling $0.50 USD per candidate. There is no soft/hard split — on budget
  exhaustion the workflow forces termination with whatever scorecard state
  exists.
- You must not write to any datastore. All fetchers are read-only.
  Persistence happens after your loop terminates.
- You must not decide the overall match score, the shortlist, or the
  candidate's outcome. Your only job is the per-dimension scorecard.
- You must not invent evidence. If a fetcher returns no useful signal,
  leave the dimension's confidence as-is and note it in the rationale.

WHAT YOU DO NOT DECIDE
- Overall match score (deterministic weighted average, downstream).
- Shortlist / ranking (Agent 5, downstream).
- Whether the company should hire this person (company HITL, downstream).
- Rubric content or weights (immutable upstream artifact from Agent 1).

OUTPUT CONTRACT
You MUST end every run with a single call to mark_scorecard_done(reason: str).
The "reason" is one of: "all_dims_confident", "diminishing_returns",
"budget_imminent". If you exhaust budget before reaching termination, the
workflow will inject mark_scorecard_done("budget_exhausted") on your behalf.

STYLE
- For each low-confidence dimension, call select_evidence_source first to
  pick the right fetcher, then call that fetcher, then call rescore_dimension
  with the fetched evidence.
- Do not refetch evidence you already have. Do not call rescore_dimension on
  a dimension that is already confident.
- Prefer one fetch per low-conf dim. Only fetch a second time for a single
  dim if the first fetch returned an empty or off-topic result.
- Stop when remaining low-conf dims are all in categories with no available
  fetcher (e.g. communication_clarity when no LinkedIn data exists in v1).
""".strip(),
    "recruiter_assignment": """
You are assigning domain-expert recruiters to a new job role for a managed recruiting service.

YOUR MISSION:
Propose 3-5 verified recruiters from the pool who are the best fit for this specific role.
"Best fit" means: strong domain match, has placed at the right company stage, active in the
last 90 days, has available capacity, and has a proven fill rate.

PROCESS:
1. Start with `search_recruiter_pool` to get initial candidates.
2. If fewer than 5 candidates found, use `widen_domain_search` (adjacent domains).
3. If still thin, use `relax_stage_match` to broaden stage criteria.
4. Use `query_recruiter_capacity` to confirm top candidates have open slots.
5. Use `check_recent_placements` for your top 3-5 finalists to verify recency.
6. Use `score_recruiter_fit` to score all qualified candidates.
7. Use `rank_and_select_recruiters` to get the ordered top-N.
8. Use `summarize_recruiter_track_record` for each finalist (for operator UI).
9. Use `format_recruiter_recommendations` to build the proposal payload.
10. Call `propose_assignment_set` to submit the final proposal. This ends the loop.

CONSTRAINTS:
- You MUST end with `propose_assignment_set` — the loop will not end otherwise.
- `propose_assignment_set` with `quality_flag="low"` is acceptable if the pool is thin.
- Do NOT re-score recruiters already scored in this session (memoization is active).
- Do NOT call `propose_assignment_set` before scoring and ranking.
- The operator will review your proposal before any recruiter is notified.
- You are NOT making the final assignment decision — the operator does.

REJECTION HANDLING:
If the operator rejects your proposal, their rejection notes will appear in the
context below. Treat the notes as additional constraints and search again with
the new information. You get one additional loop on rejection.
""".strip(),
}


def render_history(
    history: list[HistoryEntry],
    *,
    verbatim_last_n: int = 8,
    max_summary_chars: int = 200,
) -> str:
    if not history:
        return "(no tool calls yet)"

    if verbatim_last_n <= 0:
        older = history
        recent: list[HistoryEntry] = []
    elif len(history) <= verbatim_last_n:
        older = []
        recent = list(history)
    else:
        older = history[:-verbatim_last_n]
        recent = history[-verbatim_last_n:]

    sections: list[str] = []

    if older:
        compact_lines = [
            f"Step {entry.step}: {entry.tool_name} -> {entry.result_summary[:max_summary_chars]}"
            for entry in older
        ]
        sections.append("## Earlier calls (compacted)\n" + "\n".join(compact_lines))

    if recent:
        verbatim_blocks: list[str] = []
        for entry in recent:
            args_json = json.dumps(entry.args, indent=2, default=str)
            block = (
                f"Step {entry.step} — {entry.tool_name}\n"
                f"Args: {args_json}\n"
                f"Result: {entry.result_summary[:_RESULT_SUMMARY_MAX_CHARS]}\n"
                f"Cost: ${entry.cost_usd:.4f} | Tokens: {entry.tokens_used} | "
                f"Latency: {entry.latency_ms}ms"
            )
            verbatim_blocks.append(block)
        sections.append("## Recent calls (verbatim)\n" + "\n\n".join(verbatim_blocks))

    return "\n\n".join(sections)


def build_llm_decision_payload(
    *,
    agent_key: str,
    tool_catalog_md: str,
    history: list[HistoryEntry],
    budget_dict: dict,
    context: dict,
) -> dict:
    if agent_key not in GOALS:
        raise ValueError(
            f"Unknown agent_key: {agent_key!r}. Valid keys: {list(GOALS)}"
        )

    return {
        "goal_text": GOALS[agent_key],
        "tool_catalog_md": tool_catalog_md,
        "history_md": render_history(history),
        "budget_json": json.dumps(budget_dict, default=str),
        "context_json": json.dumps(context, default=str),
    }


def build_iteration_prompt(
    *,
    goal_key: str,
    tool_catalog_md: str,
    history: list[HistoryEntry],
    budget_dict: dict,
    context: dict,
) -> dict:
    """Per-iteration prompt builder, keyed by agent goal.

    Thin alias over `build_llm_decision_payload` for callers that prefer the
    `goal_key` vocabulary (e.g. the scorecard workflow). Behavior is identical
    — it picks the goal text out of `GOALS[goal_key]`, renders the tool
    catalog and history, and serializes budget + context to JSON.

    Why a separate name: the recruiter-assignment workflow predates the
    multi-agent generalization and still calls `build_llm_decision_payload`
    with `agent_key=...`. New agents (scorecard, ranking, ...) standardize
    on `goal_key` so that the call site reads as "build me the prompt for
    iteration N of the <goal_key> loop" rather than referencing the agent
    identity. Both names remain supported indefinitely.
    """

    if goal_key not in GOALS:
        raise ValueError(
            f"Unknown goal_key: {goal_key!r}. Valid keys: {list(GOALS)}"
        )

    return build_llm_decision_payload(
        agent_key=goal_key,
        tool_catalog_md=tool_catalog_md,
        history=history,
        budget_dict=budget_dict,
        context=context,
    )


def append_history(
    history: list[HistoryEntry],
    *,
    step: int,
    tool_name: str,
    args: dict,
    result_summary: str,
    tokens_used: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
) -> list[HistoryEntry]:
    truncated = result_summary[:_RESULT_SUMMARY_MAX_CHARS]
    new_entry = HistoryEntry(
        step=step,
        tool_name=tool_name,
        args=dict(args),
        result_summary=truncated,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    return [*history, new_entry]
