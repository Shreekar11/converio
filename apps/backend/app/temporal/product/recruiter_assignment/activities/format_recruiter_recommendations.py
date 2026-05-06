"""Deterministic assembly of the operator-facing proposal payload. No LLM call."""
from __future__ import annotations

from temporalio import activity

from app.schemas.product.recruiter_assignment import (
    OperatorProposal,
    ProposalEntry,
    RecruiterCandidate,
    RecruiterFitScore,
)
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_QUALITY_FLAG_VALUES: frozenset[str] = frozenset({"high", "medium", "low"})
_ARGS_SUMMARY_MAX = 300
_RESULT_SUMMARY_MAX = 300


def _truncate(value: str, max_len: int) -> str:
    """Truncate a string to `max_len` characters, preserving an empty default."""
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _build_audit_trail(history: list[dict]) -> list[ProposalEntry]:
    """Map raw tool-call history dicts into validated `ProposalEntry` rows.

    The agent loop accumulates `HistoryEntry`-shaped dicts (step, tool_name,
    args, result_summary, tokens_used, cost_usd, latency_ms). The proposal's
    `ProposalEntry` shape is similar but uses `args_summary` (a truncated
    repr) instead of the full `args` dict, and drops latency. We accept either
    shape here so the activity is robust to upstream variations and to a
    `format_recruiter_recommendations` payload built directly from already-
    summarized entries.
    """
    audit_trail: list[ProposalEntry] = []
    for raw in history:
        if not isinstance(raw, dict):
            # Defensive: history items must be dicts. Skip malformed entries
            # rather than failing the whole activity — operator review is
            # better served by a partial trail than no proposal.
            LOGGER.warning(
                "format_recruiter_recommendations.history_entry_not_dict",
                extra={"entry_type": type(raw).__name__},
            )
            continue

        # Prefer an explicit `args_summary` if the caller already built one;
        # otherwise derive a truncated repr of `args`.
        args_summary = raw.get("args_summary")
        if args_summary is None:
            args_value = raw.get("args", {})
            args_summary = repr(args_value)
        args_summary = _truncate(args_summary, _ARGS_SUMMARY_MAX)

        result_summary = _truncate(
            raw.get("result_summary", ""), _RESULT_SUMMARY_MAX
        )

        entry = ProposalEntry(
            step=int(raw.get("step", 0)),
            tool_name=str(raw.get("tool_name", "")),
            args_summary=args_summary,
            result_summary=result_summary,
            cost_usd=float(raw.get("cost_usd", 0.0) or 0.0),
            tokens_used=int(raw.get("tokens_used", 0) or 0),
        )
        audit_trail.append(entry)
    return audit_trail


def _filter_candidates(
    candidates: list[dict], ranked_ids: list[str]
) -> list[RecruiterCandidate]:
    """Return candidates matching `ranked_ids`, in ranked order.

    Builds a lookup keyed by `recruiter_id` then iterates `ranked_ids` so the
    output preserves the agent's chosen rank-order. Recruiters present in
    `ranked_ids` but missing from `candidates` are dropped (with a warning) —
    upstream invariants should have filtered the proposed set already.
    """
    by_id: dict[str, dict] = {}
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        rid = raw.get("recruiter_id")
        if rid is None:
            continue
        by_id[str(rid)] = raw

    out: list[RecruiterCandidate] = []
    for rid in ranked_ids:
        raw = by_id.get(rid)
        if raw is None:
            LOGGER.warning(
                "format_recruiter_recommendations.candidate_missing",
                extra={"recruiter_id": rid},
            )
            continue
        out.append(RecruiterCandidate.model_validate(raw))
    return out


def _filter_fit_scores(
    fit_scores: list[dict], ranked_ids: list[str]
) -> list[RecruiterFitScore]:
    """Return fit scores matching `ranked_ids`, in ranked order."""
    by_id: dict[str, dict] = {}
    for raw in fit_scores:
        if not isinstance(raw, dict):
            continue
        rid = raw.get("recruiter_id")
        if rid is None:
            continue
        by_id[str(rid)] = raw

    out: list[RecruiterFitScore] = []
    for rid in ranked_ids:
        raw = by_id.get(rid)
        if raw is None:
            LOGGER.warning(
                "format_recruiter_recommendations.fit_score_missing",
                extra={"recruiter_id": rid},
            )
            continue
        out.append(RecruiterFitScore.model_validate(raw))
    return out


def _filter_narratives(
    narratives: dict[str, str], ranked_ids: list[str]
) -> dict[str, str]:
    """Return narratives keyed only to recruiters in the proposed set."""
    out: dict[str, str] = {}
    for rid in ranked_ids:
        narrative = narratives.get(rid)
        if narrative is None:
            LOGGER.warning(
                "format_recruiter_recommendations.narrative_missing",
                extra={"recruiter_id": rid},
            )
            continue
        out[rid] = str(narrative)
    return out


def _normalize_quality_flag(value: str) -> str:
    """Coerce `quality_flag` into the allowed enum bucket; default to 'low'."""
    if isinstance(value, str) and value in _QUALITY_FLAG_VALUES:
        return value
    LOGGER.warning(
        "format_recruiter_recommendations.invalid_quality_flag",
        extra={"received": value},
    )
    return "low"


@ActivityRegistry.register("recruiter_assignment", "format_recruiter_recommendations")
@activity.defn(name="recruiter_assignment.format_recruiter_recommendations")
async def format_recruiter_recommendations(payload: dict) -> dict:
    """Assemble the final `OperatorProposal` payload for HITL review.

    Pure deterministic Python: no DB or LLM calls. Filters the candidate /
    fit-score / narrative collections to the ranked proposed set, builds the
    audit trail from raw tool-call history, and emits a JSON-mode dump ready
    for `persist_proposal`.
    """
    ranked_recruiter_ids: list[str] = [
        str(rid) for rid in payload.get("ranked_recruiter_ids", [])
    ]
    candidates_raw: list[dict] = payload.get("candidates", []) or []
    fit_scores_raw: list[dict] = payload.get("fit_scores", []) or []
    narratives_raw: dict[str, str] = payload.get("narratives", {}) or {}
    history_raw: list[dict] = payload.get("history", []) or []

    job_id: str = str(payload.get("job_id", ""))
    workflow_id: str = str(payload.get("workflow_id", ""))
    quality_flag: str = _normalize_quality_flag(
        payload.get("quality_flag", "low")
    )
    total_tool_calls: int = int(payload.get("total_tool_calls", 0) or 0)
    total_tokens: int = int(payload.get("total_tokens", 0) or 0)
    total_cost_usd: float = float(payload.get("total_cost_usd", 0.0) or 0.0)

    audit_trail = _build_audit_trail(history_raw)
    candidates = _filter_candidates(candidates_raw, ranked_recruiter_ids)
    fit_scores = _filter_fit_scores(fit_scores_raw, ranked_recruiter_ids)
    narratives = _filter_narratives(narratives_raw, ranked_recruiter_ids)

    proposal = OperatorProposal(
        job_id=job_id,
        workflow_id=workflow_id,
        proposed_recruiter_ids=ranked_recruiter_ids,
        candidates=candidates,
        fit_scores=fit_scores,
        narratives=narratives,
        audit_trail=audit_trail,
        quality_flag=quality_flag,
        total_tool_calls=total_tool_calls,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
    )

    LOGGER.info(
        "format_recruiter_recommendations.assembled",
        extra={
            "job_id": job_id,
            "workflow_id": workflow_id,
            "proposed_count": len(ranked_recruiter_ids),
            "candidates_count": len(candidates),
            "fit_scores_count": len(fit_scores),
            "narratives_count": len(narratives),
            "audit_trail_count": len(audit_trail),
            "quality_flag": quality_flag,
        },
    )

    return proposal.model_dump(mode="json")
