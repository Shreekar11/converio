"""RecruiterAssignmentWorkflow — Agent 0 (Tier 2 LLM-augmented workflow + operator HITL).

Orchestrates the recruiter matching pipeline:
  1. Adaptive LLM-directed search loop — LLM picks tools from RECRUITER_ASSIGNMENT_TOOLS,
     building up evidence until it calls the terminal `propose_assignment_set` tool.
  2. Proposal persisted to `operator_proposals` table.
  3. Workflow pauses: await operator_approval Signal (HITL Pause #1).
  4a. Approve path — assign, notify, transition job status, log event.
  4b. First reject path — re-enter search loop with rejection notes (max 1 re-loop).
  4c. Second reject — terminal REJECTED_BY_OPERATOR.

Retry policies:
  _LLM_RETRY: 3 attempts, 2x backoff, max 30s (for llm_decide_next_tool +
              score_recruiter_fit + summarize_recruiter_track_record)
  _DB_RETRY:  3 attempts, 1.5x backoff, max 10s (for all PG/Neo4j activities)

Decision 16.1: operator reject -> re-loop once with notes, then terminal.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.schemas.enums import RecruiterAssignmentStatus
    from app.schemas.product.recruiter_assignment import (
        RecruiterAssignmentInput,
        RecruiterAssignmentResult,
    )
    from app.temporal.core.workflow_registry import WorkflowRegistry, WorkflowType


# Retry policies — mirror job_intake_workflow.py shape.
# LLM activities (llm_decide_next_tool, score_recruiter_fit, summarize_recruiter_track_record)
# get exponential backoff with up to 3 attempts; DB / deterministic activities get
# faster backoff with a tighter ceiling.
_LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
    maximum_interval=timedelta(seconds=30),
)
_DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=1.5,
    maximum_attempts=3,
    maximum_interval=timedelta(seconds=10),
)

# Activities whose backing call is an LLM round-trip — they get _LLM_RETRY.
_LLM_BACKED_ACTIVITIES: frozenset[str] = frozenset(
    {
        "recruiter_assignment.score_recruiter_fit",
        "recruiter_assignment.summarize_recruiter_track_record",
    }
)


# ---------------------------------------------------------------------------
# Module-level helpers (deterministic, replay-safe — no I/O, no time, no random)
# ---------------------------------------------------------------------------


def _accumulate_result(
    tool_name: str,
    result: dict,
    candidates: dict,
    scores: dict,
    narratives: dict,
    score_memo: dict,
) -> None:
    """Merge a tool result into the per-loop accumulation dicts.

    Mutates the passed-in dicts in place. Pure, deterministic — safe to call
    inside the workflow without breaking replay semantics.
    """
    if tool_name in (
        "recruiter_assignment.search_recruiter_pool",
        "recruiter_assignment.widen_domain_search",
        "recruiter_assignment.relax_stage_match",
        "search_recruiter_pool",
        "widen_domain_search",
        "relax_stage_match",
    ):
        for c in result.get("candidates", []) or []:
            rid = c.get("recruiter_id")
            if rid:
                candidates[rid] = c

    elif tool_name in (
        "recruiter_assignment.score_recruiter_fit",
        "score_recruiter_fit",
    ):
        for s in result.get("scores", []) or []:
            rid = s.get("recruiter_id")
            if rid:
                scores[rid] = s
                score_memo[rid] = s

    elif tool_name in (
        "recruiter_assignment.summarize_recruiter_track_record",
        "summarize_recruiter_track_record",
    ):
        rec_id = result.get("recruiter_id", "")
        if rec_id:
            narratives[rec_id] = result.get("narrative", "")


def _summarize_result(tool_name: str, result: dict) -> str:
    """Build a short summary string for the history entry.

    The summary is what the LLM sees in the next decision-prompt history block,
    so we keep it terse but informative enough to drive the next choice.
    """
    if not isinstance(result, dict):
        return str(result)[:200]

    if "candidates" in result:
        return f"Returned {result.get('count', len(result.get('candidates', []) or []))} candidates"
    if "scores" in result:
        return (
            f"Scored {result.get('scored_count', len(result.get('scores', []) or []))} "
            f"recruiters ({result.get('cached_count', 0)} cached)"
        )
    if "ranked" in result:
        ranked = result.get("ranked", []) or []
        ids = [str(r.get("recruiter_id", ""))[:8] for r in ranked[:3]]
        return f"Top {len(ranked)}: {ids}"
    if "narrative" in result:
        return f"Summary ({len(result.get('narrative', ''))} chars)"
    if "capacity" in result:
        return f"Capacity for {len(result.get('capacity', []) or [])} recruiters"
    if "placements" in result:
        return f"{result.get('count', len(result.get('placements', []) or []))} placements"
    return str(result)[:200]


@WorkflowRegistry.register(category=WorkflowType.BUSINESS, task_queue="converio-queue")
@workflow.defn(name="RecruiterAssignmentWorkflow")
class RecruiterAssignmentWorkflow:
    """Agent 0 — adaptive recruiter matching with operator HITL gate."""

    def __init__(self) -> None:
        self._current_phase: str = "initialized"
        self._job_id: str | None = None
        self._signal_received: bool = False
        self._signal_payload: dict | None = None
        self._reject_count: int = 0  # tracks re-loop count (max 1, per Decision 16.1)
        self._status: str = RecruiterAssignmentStatus.PROPOSED.value

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    @workflow.query(name="current_phase")
    def current_phase(self) -> str:
        """Return the current orchestration phase string."""
        return self._current_phase

    @workflow.query
    def get_status(self) -> dict:
        """Return aggregate workflow state for live observability."""
        return {
            "phase": self._current_phase,
            "job_id": self._job_id,
            "status": self._status,
            "reject_count": self._reject_count,
        }

    # ------------------------------------------------------------------
    # Signal handler
    # ------------------------------------------------------------------

    @workflow.signal(name="operator_approval")
    def operator_approval(self, payload: dict) -> None:
        """Receive operator decision (approve / reject) from the API layer.

        Stores the payload and flips the wait flag — the main `run` coroutine
        is parked on `wait_condition(self._signal_received)` and will resume
        on the next event-loop tick.
        """
        self._signal_payload = payload
        self._signal_received = True

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        """Execute the recruiter-assignment pipeline end-to-end.

        Args:
            input_data: JSON-serializable dict matching RecruiterAssignmentInput.

        Returns:
            JSON-serializable dict matching RecruiterAssignmentResult.
        """
        # All non-Temporal imports must happen inside the sandbox-passthrough
        # block; mirrors the job_intake_workflow pattern.
        with workflow.unsafe.imports_passed_through():
            from app.temporal.core.budget import Budget  # noqa: F401
            from app.temporal.core.execute_tool import (  # noqa: F401
                get_tool_timeout,
                is_terminal_tool,
                resolve_tool_call,
            )
            from app.temporal.core.prompt_assembly import (  # noqa: F401
                HistoryEntry,
                append_history,
                build_llm_decision_payload,
            )
            from app.temporal.core.tool_registry import (  # noqa: F401
                RECRUITER_ASSIGNMENT_TOOLS,
                render_tools_for_llm,
            )

        inp = RecruiterAssignmentInput.model_validate(input_data)
        self._job_id = inp.job_id

        workflow.logger.info(
            "RecruiterAssignmentWorkflow starting",
            extra={"job_id": inp.job_id},
        )

        # Build the tool catalog once; reused across the (up to 2) search loops.
        tool_catalog_md = render_tools_for_llm(RECRUITER_ASSIGNMENT_TOOLS)

        # Stash modules on self so the helper methods can reach them without
        # re-importing inside another sandbox block.
        self._mods = {
            "Budget": Budget,
            "HistoryEntry": HistoryEntry,
            "append_history": append_history,
            "build_llm_decision_payload": build_llm_decision_payload,
            "resolve_tool_call": resolve_tool_call,
            "get_tool_timeout": get_tool_timeout,
            "is_terminal_tool": is_terminal_tool,
        }

        # ------------------------------------------------------------------
        # First search loop -> proposal
        # ------------------------------------------------------------------
        proposal, all_scores = await self._run_search_loop(
            inp=inp,
            tool_catalog_md=tool_catalog_md,
            rejection_notes=inp.rejection_notes,
        )

        # Persist + wait on operator (HITL Pause #1).
        signal_data = await self._persist_and_wait_for_signal(inp=inp, proposal=proposal)
        decision = signal_data.get("decision", "approve")

        # ------------------------------------------------------------------
        # Decision branching
        # ------------------------------------------------------------------
        if decision == "reject" and self._reject_count == 0:
            # Decision 16.1: re-enter loop once with operator's rejection notes.
            self._reject_count += 1
            rejection_notes = signal_data.get("notes") or "No reason provided"
            workflow.logger.info(
                "Operator rejected proposal, re-entering search loop",
                extra={
                    "job_id": inp.job_id,
                    "notes_len": len(rejection_notes),
                },
            )

            proposal, all_scores = await self._run_search_loop(
                inp=inp,
                tool_catalog_md=tool_catalog_md,
                rejection_notes=rejection_notes,
            )

            signal_data = await self._persist_and_wait_for_signal(
                inp=inp, proposal=proposal
            )
            decision = signal_data.get("decision", "approve")

        if decision == "reject":
            # Second rejection (or first when _reject_count is already 1) — terminal.
            return await self._handle_rejection(
                inp=inp, proposal=proposal, signal_data=signal_data
            )

        # decision == "approve"
        return await self._handle_approval(
            inp=inp,
            proposal=proposal,
            signal_data=signal_data,
            all_scores=all_scores,
        )

    # ------------------------------------------------------------------
    # Adaptive LLM-directed search loop
    # ------------------------------------------------------------------

    async def _run_search_loop(
        self,
        *,
        inp: "RecruiterAssignmentInput",
        tool_catalog_md: str,
        rejection_notes: str | None = None,
    ) -> tuple[dict, dict[str, dict]]:
        """Run one full LLM-directed search loop until terminal tool or budget exhaustion.

        Returns:
            (proposal_dict, all_scores_by_recruiter_id) — `all_scores` is needed
            by the approve path so it can pass `fit_scores` to assignment.
        """
        Budget = self._mods["Budget"]
        HistoryEntry = self._mods["HistoryEntry"]  # noqa: F841
        append_history = self._mods["append_history"]
        build_llm_decision_payload = self._mods["build_llm_decision_payload"]
        resolve_tool_call = self._mods["resolve_tool_call"]
        get_tool_timeout = self._mods["get_tool_timeout"]
        is_terminal_tool = self._mods["is_terminal_tool"]

        budget = Budget()
        history: list = []
        all_candidates: dict[str, dict] = {}
        all_scores: dict[str, dict] = {}
        all_narratives: dict[str, str] = {}
        ranked_ids: list[str] = []
        score_memo: dict[str, dict] = {}

        context = {
            "job_id": inp.job_id,
            "classification": inp.classification,
            "rubric": inp.rubric,
            "params": inp.params.model_dump(),
            "rejection_notes": rejection_notes or "",
        }

        self._current_phase = "searching"
        self._status = RecruiterAssignmentStatus.PROPOSED.value
        step = 0

        while True:
            # ---- Hard-budget check: synthesize best-effort proposal and exit. ----
            if budget.should_terminate():
                workflow.logger.warning(
                    "Budget hard-exhausted, synthesizing best-effort proposal",
                    extra={
                        "job_id": inp.job_id,
                        "tool_calls": budget._tool_calls,
                        "tokens": budget._tokens,
                        "cost_usd": budget._cost_usd,
                    },
                )
                proposal = await workflow.execute_activity(
                    "recruiter_assignment.synthesize_best_effort_proposal",
                    {
                        "history": [h.model_dump() for h in history],
                        "job_id": inp.job_id,
                        "workflow_id": workflow.info().workflow_id,
                        "top_n": inp.params.top_n,
                        "total_tool_calls": budget._tool_calls,
                        "total_tokens": budget._tokens,
                        "total_cost_usd": budget._cost_usd,
                    },
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=_DB_RETRY,
                )
                return proposal, all_scores

            # ---- Ask the LLM for the next tool. ----
            llm_payload = build_llm_decision_payload(
                agent_key="recruiter_assignment",
                tool_catalog_md=tool_catalog_md,
                history=history,
                budget_dict=budget.serialize(),
                context=context,
            )
            decision_raw: dict = await workflow.execute_activity(
                "core.llm_decide_next_tool",
                llm_payload,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_LLM_RETRY,
            )

            tool_name = decision_raw.get("tool_name", "")
            tool_args = decision_raw.get("args", {}) or {}

            # ---- Terminal tool: build and return the formatted proposal. ----
            if is_terminal_tool(tool_name):
                proposal_dict = await self._finalize_proposal(
                    inp=inp,
                    tool_args=tool_args,
                    all_candidates=all_candidates,
                    all_scores=all_scores,
                    all_narratives=all_narratives,
                    ranked_ids=ranked_ids,
                    history=history,
                    budget=budget,
                )
                return proposal_dict, all_scores

            # ---- Resolve + dispatch tool. ----
            activity_name, validated_args = resolve_tool_call(tool_name, tool_args)

            # Memoize prior scores so the activity can skip re-scoring already-known recruiters.
            if tool_name in ("score_recruiter_fit", "recruiter_assignment.score_recruiter_fit"):
                validated_args = dict(validated_args)
                validated_args["memo"] = score_memo

            retry_policy = (
                _LLM_RETRY if activity_name in _LLM_BACKED_ACTIVITIES else _DB_RETRY
            )

            result_raw: Any = await workflow.execute_activity(
                activity_name,
                validated_args,
                start_to_close_timeout=get_tool_timeout(activity_name),
                retry_policy=retry_policy,
            )

            # Defensive: activities must return dict; if they ever drift, coerce safely.
            if not isinstance(result_raw, dict):
                result_raw = {"value": result_raw}

            # ---- Accumulate per-tool side data. ----
            _accumulate_result(
                tool_name=activity_name,
                result=result_raw,
                candidates=all_candidates,
                scores=all_scores,
                narratives=all_narratives,
                score_memo=score_memo,
            )
            if activity_name == "recruiter_assignment.rank_and_select_recruiters":
                ranked_ids = [
                    r["recruiter_id"]
                    for r in (result_raw.get("ranked", []) or [])
                    if isinstance(r, dict) and r.get("recruiter_id")
                ]

            # ---- Charge the budget for this iteration. ----
            budget.charge(
                tokens=int(decision_raw.get("tokens_used", 0) or 0),
                cost_usd=float(decision_raw.get("cost_usd", 0.0) or 0.0),
            )

            # ---- Append a compact history entry the next LLM call will see. ----
            result_summary = _summarize_result(activity_name, result_raw)
            history = append_history(
                history,
                step=step,
                tool_name=tool_name,
                args={k: str(v)[:100] for k, v in (tool_args or {}).items()},
                result_summary=result_summary,
                tokens_used=int(decision_raw.get("tokens_used", 0) or 0),
                cost_usd=float(decision_raw.get("cost_usd", 0.0) or 0.0),
                latency_ms=int(decision_raw.get("latency_ms", 0) or 0),
            )
            step += 1

    # ------------------------------------------------------------------
    # Helpers — proposal finalization, persist+wait, terminal branches
    # ------------------------------------------------------------------

    async def _finalize_proposal(
        self,
        *,
        inp: "RecruiterAssignmentInput",
        tool_args: dict,
        all_candidates: dict[str, dict],
        all_scores: dict[str, dict],
        all_narratives: dict[str, str],
        ranked_ids: list[str],
        history: list,
        budget: Any,
    ) -> dict:
        """Format the LLM's chosen proposal set into the operator-facing payload."""
        proposed_ids = (
            tool_args.get("recruiter_ids")
            or ranked_ids
            or list(all_candidates.keys())[: inp.params.top_n]
        )
        quality_flag = tool_args.get("quality_flag", "high")
        rationale = tool_args.get("rationale", "")  # noqa: F841 — surfaced via audit history

        proposal = await workflow.execute_activity(
            "recruiter_assignment.format_recruiter_recommendations",
            {
                "ranked_recruiter_ids": proposed_ids,
                "candidates": list(all_candidates.values()),
                "fit_scores": list(all_scores.values()),
                "narratives": all_narratives,
                "history": [
                    {
                        "step": h.step,
                        "tool_name": h.tool_name,
                        "args_summary": str(h.args)[:300],
                        "result_summary": h.result_summary[:300],
                        "cost_usd": h.cost_usd,
                        "tokens_used": h.tokens_used,
                    }
                    for h in history
                ],
                "job_id": inp.job_id,
                "workflow_id": workflow.info().workflow_id,
                "quality_flag": quality_flag,
                "total_tool_calls": budget._tool_calls,
                "total_tokens": budget._tokens,
                "total_cost_usd": budget._cost_usd,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_DB_RETRY,
        )
        return proposal

    async def _persist_and_wait_for_signal(
        self,
        *,
        inp: "RecruiterAssignmentInput",
        proposal: dict,
    ) -> dict:
        """Persist the proposal and pause until an operator_approval signal arrives.

        Used in both the first-pass and re-loop paths. Resets the signal-received
        flag so a subsequent re-loop can park on the same condition cleanly.
        """
        self._current_phase = "persist_proposal"
        await workflow.execute_activity(
            "recruiter_assignment.persist_proposal",
            {
                "job_id": inp.job_id,
                "workflow_id": workflow.info().workflow_id,
                "proposal": proposal,
                "quality_flag": proposal.get("quality_flag", "high"),
            },
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_DB_RETRY,
        )

        self._current_phase = "awaiting_operator"
        self._status = RecruiterAssignmentStatus.AWAITING_OPERATOR.value

        await workflow.wait_condition(lambda: self._signal_received)
        self._signal_received = False
        signal_data = self._signal_payload or {}
        return signal_data

    async def _handle_rejection(
        self,
        *,
        inp: "RecruiterAssignmentInput",
        proposal: dict,
        signal_data: dict,
    ) -> dict:
        """Terminal path — operator rejected (and re-loop already exhausted)."""
        self._status = RecruiterAssignmentStatus.REJECTED_BY_OPERATOR.value
        self._current_phase = "rejected"

        await workflow.execute_activity(
            "recruiter_assignment.log_hitl_event",
            {
                "job_id": inp.job_id,
                "workflow_id": workflow.info().workflow_id,
                "signal_type": "operator_approval",
                "actor_type": "operator",
                "actor_id": signal_data.get(
                    "operator_id", "00000000-0000-0000-0000-000000000000"
                ),
                "action": "reject",
                "event_payload": signal_data,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_DB_RETRY,
        )

        workflow.logger.info(
            "RecruiterAssignmentWorkflow terminal: rejected_by_operator",
            extra={"job_id": inp.job_id, "reject_count": self._reject_count},
        )

        return RecruiterAssignmentResult(
            job_id=inp.job_id,
            status=RecruiterAssignmentStatus.REJECTED_BY_OPERATOR.value,
            assigned_recruiter_ids=[],
            assignment_count=0,
            proposal=proposal,  # type: ignore[arg-type]
            operator_decision=signal_data,
            total_loop_iterations=self._reject_count + 1,
            total_cost_usd=0.0,
            total_tokens=0,
        ).model_dump(mode="json")

    async def _handle_approval(
        self,
        *,
        inp: "RecruiterAssignmentInput",
        proposal: dict,
        signal_data: dict,
        all_scores: dict[str, dict],
    ) -> dict:
        """Approve path — assign, notify, transition status, log, return result."""
        override_set = signal_data.get("override_set") or []
        confirmed_ids = (
            list(override_set)
            if override_set
            else (
                signal_data.get("confirmed_recruiter_ids")
                or proposal.get("proposed_recruiter_ids", [])
            )
        )
        operator_id = signal_data.get(
            "operator_id", "00000000-0000-0000-0000-000000000000"
        )
        is_override = bool(override_set)

        # 1. Assign recruiters to the role (single-tx INSERT AssignmentRecommendation rows).
        self._current_phase = "assigning"
        await workflow.execute_activity(
            "recruiter_assignment.assign_recruiters_to_role",
            {
                "job_id": inp.job_id,
                "confirmed_recruiter_ids": confirmed_ids,
                "fit_scores": list(all_scores.values()),
                "operator_id": operator_id,
                "operator_override": is_override,
            },
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=_DB_RETRY,
        )

        # 2. Notify the assigned recruiters (idempotent at activity layer).
        self._current_phase = "notifying"
        await workflow.execute_activity(
            "recruiter_assignment.notify_assigned_recruiters",
            {
                "recruiter_ids": confirmed_ids,
                "job_id": inp.job_id,
                "job_title": inp.classification.get("role_category"),
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # 3. Transition the parent Job to the next pipeline stage.
        self._current_phase = "transitioning_status"
        await workflow.execute_activity(
            "recruiter_assignment.transition_job_status",
            {
                "job_id": inp.job_id,
                "from_status": "recruiter_assignment",
                "to_status": "sourcing",
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_DB_RETRY,
        )

        # 4. Audit the HITL approve event.
        await workflow.execute_activity(
            "recruiter_assignment.log_hitl_event",
            {
                "job_id": inp.job_id,
                "workflow_id": workflow.info().workflow_id,
                "signal_type": "operator_approval",
                "actor_type": "operator",
                "actor_id": operator_id,
                "action": "approve",
                "event_payload": signal_data,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_DB_RETRY,
        )

        self._status = RecruiterAssignmentStatus.ASSIGNED.value
        self._current_phase = "completed"

        workflow.logger.info(
            "RecruiterAssignmentWorkflow completed: assigned",
            extra={
                "job_id": inp.job_id,
                "assignment_count": len(confirmed_ids),
                "loop_iterations": self._reject_count + 1,
                "operator_override": is_override,
            },
        )

        return RecruiterAssignmentResult(
            job_id=inp.job_id,
            status=RecruiterAssignmentStatus.ASSIGNED.value,
            assigned_recruiter_ids=confirmed_ids,
            assignment_count=len(confirmed_ids),
            proposal=proposal,  # type: ignore[arg-type]
            operator_decision=signal_data,
            total_loop_iterations=self._reject_count + 1,
            total_cost_usd=0.0,  # cross-loop budget aggregation is future work
            total_tokens=0,
        ).model_dump(mode="json")
