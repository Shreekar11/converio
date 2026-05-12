"""Best-effort proposal builder used when budget is hard-exhausted before the LLM
calls propose_assignment_set. Scans accumulated history for highest-scored
recruiters seen and constructs a low-quality proposal. No LLM or DB call."""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from temporalio import activity

from app.schemas.product.recruiter_assignment import (
    OperatorProposal,
    RecruiterCandidate,
    RecruiterFitScore,
)
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_SCORE_TOOL = "score_recruiter_fit"
_SEARCH_TOOLS = (
    "search_recruiter_pool",
    "widen_domain_search",
    "relax_stage_match",
)


def _maybe_load_json(raw: Any) -> Any | None:
    """Best-effort JSON parse. Returns None on failure or non-string input.

    `result_summary` is truncated upstream (see prompt_assembly), so a JSON
    blob may be cut mid-token. We attempt a parse but never raise — callers
    must treat None as "no structured data available".
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    # Cheap shape check before paying the json.loads cost.
    if stripped[0] not in ("{", "["):
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _coerce_iter(value: Any) -> list[Any]:
    """Normalize a parsed JSON value into a list of dict-like items."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        # Some tool payloads wrap the list under common keys.
        for key in ("scores", "results", "candidates", "items", "data"):
            inner = value.get(key)
            if isinstance(inner, list):
                return [v for v in inner if isinstance(v, dict)]
        # A single dict counts as a one-item collection.
        return [value]
    return []


def _extract_scores(parsed: Any) -> list[RecruiterFitScore]:
    """Pull as many valid RecruiterFitScore objects out of a parsed payload as we can.

    Silently drops malformed entries — we are running in a degraded mode and
    must never crash the workflow on bad history.
    """
    scores: list[RecruiterFitScore] = []
    for item in _coerce_iter(parsed):
        try:
            scores.append(RecruiterFitScore.model_validate(item))
        except ValidationError:
            continue
    return scores


def _extract_candidates(parsed: Any) -> list[RecruiterCandidate]:
    candidates: list[RecruiterCandidate] = []
    for item in _coerce_iter(parsed):
        try:
            candidates.append(RecruiterCandidate.model_validate(item))
        except ValidationError:
            continue
    return candidates


@ActivityRegistry.register(
    "recruiter_assignment", "synthesize_best_effort_proposal"
)
@activity.defn(name="recruiter_assignment.synthesize_best_effort_proposal")
async def synthesize_best_effort_proposal(payload: dict) -> dict:
    """Build a degraded-mode OperatorProposal from accumulated tool-call history.

    Invoked when the agent's token/cost budget is exhausted before the LLM
    has had a chance to call `propose_assignment_set`. We scan whatever the
    agent managed to gather (scores, candidates) and stitch a `quality_flag=
    "low"` proposal so the operator still sees *something* to triage rather
    than a hard workflow failure.
    """
    history: list[dict] = payload.get("history") or []
    job_id: str = str(payload.get("job_id", ""))
    workflow_id: str = str(payload.get("workflow_id", ""))
    top_n: int = int(payload.get("top_n", 5) or 5)
    total_tool_calls: int = int(payload.get("total_tool_calls", 0) or 0)
    total_tokens: int = int(payload.get("total_tokens", 0) or 0)
    total_cost_usd: float = float(payload.get("total_cost_usd", 0.0) or 0.0)

    # Highest-score-wins memoization: a single recruiter may appear in many
    # score_recruiter_fit calls (re-scoring after widen/relax). Keep the best.
    score_by_recruiter: dict[str, RecruiterFitScore] = {}
    candidate_by_recruiter: dict[str, RecruiterCandidate] = {}
    candidate_order: list[str] = []  # preserve discovery order for tie-break

    for raw_entry in history:
        if not isinstance(raw_entry, dict):
            continue
        tool_name = raw_entry.get("tool_name")
        if not isinstance(tool_name, str):
            continue

        result_summary = raw_entry.get("result_summary")
        parsed = _maybe_load_json(result_summary)
        if parsed is None:
            continue

        if tool_name == _SCORE_TOOL:
            for score in _extract_scores(parsed):
                existing = score_by_recruiter.get(score.recruiter_id)
                if existing is None or score.score > existing.score:
                    score_by_recruiter[score.recruiter_id] = score
        elif tool_name in _SEARCH_TOOLS:
            for cand in _extract_candidates(parsed):
                if cand.recruiter_id not in candidate_by_recruiter:
                    candidate_by_recruiter[cand.recruiter_id] = cand
                    candidate_order.append(cand.recruiter_id)

    # Selection strategy: prefer scored recruiters (we know they ranked well
    # against the rubric), then fall back to first-seen candidates so the
    # operator at least gets a list. Cap at top_n.
    if score_by_recruiter:
        ranked_ids = [
            rid
            for rid, _ in sorted(
                score_by_recruiter.items(),
                key=lambda kv: kv[1].score,
                reverse=True,
            )
        ][:top_n]
    else:
        ranked_ids = candidate_order[:top_n]

    selected_candidates: list[RecruiterCandidate] = []
    selected_scores: list[RecruiterFitScore] = []
    final_ids: list[str] = []
    for rid in ranked_ids:
        cand = candidate_by_recruiter.get(rid)
        if cand is None:
            # We have a score but never saw a candidate row for this recruiter
            # (e.g. score arrived from a payload we couldn't parse fully). Drop
            # rather than fabricate — the proposal must be self-contained per
            # the OperatorProposal contract (candidates align with ids).
            continue
        selected_candidates.append(cand)
        final_ids.append(rid)
        score = score_by_recruiter.get(rid)
        if score is not None:
            selected_scores.append(score)

    LOGGER.warning(
        "best_effort_proposal_synthesized",
        extra={
            "job_id": job_id,
            "workflow_id": workflow_id,
            "history_entries": len(history),
            "scored_recruiters_seen": len(score_by_recruiter),
            "candidates_seen": len(candidate_by_recruiter),
            "proposal_size": len(final_ids),
            "total_tool_calls": total_tool_calls,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
        },
    )

    proposal = OperatorProposal(
        job_id=job_id,
        workflow_id=workflow_id,
        proposed_recruiter_ids=final_ids,
        candidates=selected_candidates,
        fit_scores=selected_scores,
        narratives={},
        audit_trail=[],
        quality_flag="low",
        total_tool_calls=total_tool_calls,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
    )
    return proposal.model_dump(mode="json")
