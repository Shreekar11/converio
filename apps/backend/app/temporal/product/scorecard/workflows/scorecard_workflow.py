"""ScorecardGeneratorWorkflow — Agent 4 (Tier 3 autonomous agent).

Per-candidate child workflow that produces an evidence-backed scorecard
against the immutable role rubric.

End-to-end pipeline (plan §7):

  1. load candidate profile (DB)
  2. load job + rubric (DB)
  3. build initial scoring prompt (deterministic)
  4. score_candidate_dimensions (PRIMARY LLM — Pro tier)
  5. check_confidence_gate (deterministic — filters low-conf dims)
  6. REFLECTIVE LOOP (budgeted, LLM-directed):
       * llm_decide_next_tool picks: select_evidence_source / a fetcher /
         rescore_dimension / mark_scorecard_done.
       * Workflow dispatches the chosen activity, merges results back
         into the working scorecard.
       * Loop exits when:
           a) LLM emits mark_scorecard_done, OR
           b) Budget is exhausted (tool_calls=8, wallclock=180s,
              cost=$0.50) — workflow injects
              mark_scorecard_done("budget_exhausted").
  7. compute_overall_match_score (deterministic, Decimal-exact)
  8. resolve_citations (3-tier, deterministic)
  9. persist_scorecard (idempotent upsert)

The reflective loop is the Tier 3 differentiator — the LLM picks both
WHICH evidence source to consult AND WHEN to stop, bounded by a budget
the workflow enforces. See plan §5 + §11 for the Anthropic-taxonomy
discussion (Agent, not Workflow).

Per-candidate budget (plan §11):
  - Tool calls: hard limit 8 (soft warn at 6)
  - Wallclock: 180s hard (150s soft)
  - LLM cost: $0.50 hard ($0.40 soft)

Idempotency: ID = `scorecard-{job_id}-{candidate_id}` with REJECT_DUPLICATE
ID-reuse policy. The downstream persist_scorecard activity upserts on
`(job_id, candidate_id, rubric_id)`, so re-running under a new rubric
version is safe and produces a fresh ScorecardWorkflowResult while
overwriting the same DB row.

Retry policies mirror the sibling RecruiterAssignmentWorkflow:
  - LLM activities  (score / select / rescore / decide): 3 attempts,
    2x backoff, max 30s — LLM transports flake regularly enough that
    a tight retry is worth the latency.
  - DB / deterministic: 3 attempts, 1.5x backoff, max 10s.

Replay-determinism:
  - No `datetime.now()` — `workflow.now()` is the only allowed clock.
  - All side-effecting code lives in activities; the workflow only
    branches on activity results.
  - `with workflow.unsafe.imports_passed_through():` wraps non-stdlib
    imports so the workflow sandbox does not re-import them at every
    decision.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.schemas.product.scorecard import (
        ScorecardWorkflowInput,
        ScorecardWorkflowResult,
    )
    from app.temporal.core.workflow_registry import WorkflowRegistry, WorkflowType


# ---------------------------------------------------------------------------
# Retry policies — mirror recruiter_assignment_workflow shape exactly so
# operator runbooks stay symmetric across Tier 2 / Tier 3 agents.
# ---------------------------------------------------------------------------
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
# Everything else uses _DB_RETRY (or deterministic, where the policy is
# irrelevant). The dispatcher inside the reflective loop picks via this set.
_LLM_BACKED_ACTIVITIES: frozenset[str] = frozenset(
    {
        "scorecard.score_candidate_dimensions",
        "scorecard.select_evidence_source",
        "scorecard.rescore_dimension",
        "core.llm_decide_next_tool",
    }
)


# Termination reasons surfaced on the ScorecardWorkflowResult. Kept as
# module constants so the type checker catches typos at call sites and
# the values stay symmetric with `MarkScorecardDoneInput.reason`.
_REASON_ALL_DIMS_CONFIDENT = "all_dims_confident"
_REASON_DIMINISHING_RETURNS = "diminishing_returns"
_REASON_BUDGET_IMMINENT = "budget_imminent"
_REASON_BUDGET_EXHAUSTED = "budget_exhausted"


# Phases surfaced via the `current_phase` query handler for Temporal UI
# polling. Symbolic constants instead of bare strings so tooling that
# scrapes phases (dashboards, alerts) sees the full enum in code search.
_PHASE_INITIALIZED = "initialized"
_PHASE_LOADING_CONTEXT = "loading_context"
_PHASE_INITIAL_SCORING = "initial_scoring"
_PHASE_GATE_CHECK = "gate_check"
_PHASE_SELF_CORRECTION = "self_correction"
_PHASE_COMPUTE_OVERALL = "compute_overall"
_PHASE_RESOLVE_CITATIONS = "resolve_citations"
_PHASE_PERSIST = "persist"
_PHASE_DONE = "done"


@WorkflowRegistry.register(category=WorkflowType.BUSINESS, task_queue="converio-queue")
@workflow.defn(name="ScorecardGeneratorWorkflow")
class ScorecardGeneratorWorkflow:
    """Agent 4 — Tier 3 autonomous scorecard generator.

    Per-candidate child workflow. Runs initial scoring → confidence
    gate → reflective evidence-gathering loop (LLM-directed) → weighted
    overall score → citation resolution → persist.
    """

    def __init__(self) -> None:
        # `_current_phase` drives the `current_phase` query handler.
        # All assignments to it happen inside `run` so the value is
        # serialized lock-step with workflow state for replay correctness.
        self._current_phase: str = _PHASE_INITIALIZED

        # IDs are stashed on self so query handlers can surface them
        # without re-parsing the input dict.
        self._job_id: str | None = None
        self._candidate_id: str | None = None
        self._rubric_id: str | None = None

        # Track whether the reflective loop ever ran (>=1 iteration of
        # llm_decide_next_tool). Drives the persisted
        # `self_correction_triggered` flag on the scorecard row.
        self._self_correction_triggered: bool = False

        # Names of dimensions that the LLM successfully rescored. We
        # track these so the persisted result records the exact
        # delta-vs-initial — the operator UI uses this to render
        # "this dim was rescored" badges.
        self._dimensions_rescored: list[str] = []

        # Aggregate cost / call counters. Charged from llm_decide_next_tool
        # results AND from the initial scoring call. Tool-call count
        # tracks reflective-loop calls only (initial scoring is one
        # PRIMARY LLM call, not a "tool call" from the budget's POV;
        # see plan §11).
        self._tool_call_count: int = 0
        self._total_cost_usd: float = 0.0
        self._total_tokens: int = 0

    # ------------------------------------------------------------------
    # Query handlers (replay-safe — read-only on workflow state)
    # ------------------------------------------------------------------

    @workflow.query(name="current_phase")
    def current_phase(self) -> str:
        """Return the current orchestration phase string."""
        return self._current_phase

    @workflow.query
    def get_status(self) -> dict:
        """Return aggregate workflow state for live observability.

        Used by the operator UI / Temporal admin tools to render
        per-candidate scorecard progress without scraping event history.
        """
        return {
            "phase": self._current_phase,
            "job_id": self._job_id,
            "candidate_id": self._candidate_id,
            "rubric_id": self._rubric_id,
            "self_correction_triggered": self._self_correction_triggered,
            "dimensions_rescored": list(self._dimensions_rescored),
            "tool_call_count": self._tool_call_count,
            "total_cost_usd": round(self._total_cost_usd, 4),
        }

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        """Execute the scorecard pipeline end-to-end for a single candidate.

        Args:
            input_data: JSON-serializable dict matching
                `ScorecardWorkflowInput` (job_id, candidate_id,
                rubric_id, optional submission_id).

        Returns:
            JSON-serializable dict matching `ScorecardWorkflowResult`.

        Raises:
            ApplicationError: on unrecoverable preconditions (missing
                candidate, missing rubric). Temporal surfaces these as
                terminal workflow failures.
        """
        # Sandbox-passthrough imports for everything non-stdlib. Mirrors
        # the recruiter_assignment_workflow pattern so future agents
        # have one boilerplate to copy.
        with workflow.unsafe.imports_passed_through():
            from app.temporal.core.budget import Budget
            from app.temporal.core.execute_tool import (
                get_tool_timeout,
                is_terminal_tool,
                resolve_tool_call,
            )
            from app.temporal.core.prompt_assembly import (
                HistoryEntry,
                append_history,
                build_iteration_prompt,
            )
            from app.temporal.core.tool_registry import render_tools_for_llm
            from app.temporal.product.scorecard.tool_registry import (
                SCORECARD_TOOLS,
            )

        inp = ScorecardWorkflowInput.model_validate(input_data)
        self._job_id = str(inp.job_id)
        self._candidate_id = str(inp.candidate_id)
        self._rubric_id = str(inp.rubric_id)

        workflow.logger.info(
            "ScorecardGeneratorWorkflow starting",
            extra={
                "job_id": self._job_id,
                "candidate_id": self._candidate_id,
                "rubric_id": self._rubric_id,
                "submission_id": str(inp.submission_id) if inp.submission_id else None,
                "workflow_id": workflow.info().workflow_id,
            },
        )

        # ------------------------------------------------------------------
        # 1-2. Load candidate + job/rubric context.
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_LOADING_CONTEXT

        candidate = await self._load_candidate(self._candidate_id)
        job_rubric = await self._load_job_rubric(self._job_id, self._rubric_id)

        # ------------------------------------------------------------------
        # 3. Build the initial scoring prompt (deterministic).
        # ------------------------------------------------------------------
        scoring_prompt_result = await workflow.execute_activity(
            "scorecard.build_scoring_prompt",
            {
                "candidate_profile_json": candidate.get("enriched_data", {}),
                "job_description": job_rubric["job_description"],
                "intake_notes": job_rubric.get("intake_notes"),
                "rubric_json": {"dimensions": job_rubric["dimensions"]},
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # ------------------------------------------------------------------
        # 4. PRIMARY LLM CALL — score every rubric dim in one shot.
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_INITIAL_SCORING
        score_result = await workflow.execute_activity(
            "scorecard.score_candidate_dimensions",
            {
                "scoring_prompt": scoring_prompt_result["prompt"],
                "rubric_dimensions": job_rubric["dimensions"],
            },
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_LLM_RETRY,
        )

        scorecard_output: dict[str, Any] = score_result["scorecard_output"]
        # Initial LLM cost lands on the workflow's running total. We
        # don't increment `tool_call_count` for the primary scoring
        # call — that counter is reserved for reflective-loop tool
        # calls per plan §11.
        self._total_cost_usd += float(score_result.get("cost_usd", 0.0) or 0.0)
        self._total_tokens += int(score_result.get("tokens_used", 0) or 0)

        # ------------------------------------------------------------------
        # 5. Confidence gate.
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_GATE_CHECK
        gate = await workflow.execute_activity(
            "scorecard.check_confidence_gate",
            {"dimensions": scorecard_output["dimensions"]},
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_DB_RETRY,
        )

        # ------------------------------------------------------------------
        # 6. Reflective loop (only if there are low-conf dims).
        # ------------------------------------------------------------------
        termination_reason: str
        if gate.get("all_confident", False):
            # Initial scoring was already confident on every dim.
            # No reflective loop needed; the LLM's implied terminate
            # reason is `all_dims_confident`. We do NOT flip
            # `self_correction_triggered` because the loop never ran.
            termination_reason = _REASON_ALL_DIMS_CONFIDENT
            workflow.logger.info(
                "Initial scorecard fully confident; skipping reflective loop",
                extra={
                    "job_id": self._job_id,
                    "candidate_id": self._candidate_id,
                    "dim_count": len(scorecard_output.get("dimensions", []) or []),
                },
            )
        else:
            self._current_phase = _PHASE_SELF_CORRECTION
            self._self_correction_triggered = True

            scorecard_output, termination_reason = await self._run_reflective_loop(
                Budget=Budget,
                HistoryEntry=HistoryEntry,
                append_history=append_history,
                build_iteration_prompt=build_iteration_prompt,
                render_tools_for_llm=render_tools_for_llm,
                resolve_tool_call=resolve_tool_call,
                get_tool_timeout=get_tool_timeout,
                is_terminal_tool=is_terminal_tool,
                tool_specs=SCORECARD_TOOLS,
                candidate=candidate,
                job_rubric=job_rubric,
                scorecard_output=scorecard_output,
                initial_gate=gate,
            )

        # ------------------------------------------------------------------
        # 7. Compute the deterministic overall score.
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_COMPUTE_OVERALL
        overall_result = await workflow.execute_activity(
            "scorecard.compute_overall_match_score",
            {"dimensions": scorecard_output["dimensions"]},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_DB_RETRY,
        )
        overall_score_str: str = overall_result["overall_match_score"]

        # ------------------------------------------------------------------
        # 8. Citation resolution (3-tier).
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_RESOLVE_CITATIONS
        resolved = await workflow.execute_activity(
            "scorecard.resolve_citations",
            {
                "dimensions": scorecard_output["dimensions"],
                "candidate_profile_text": candidate["profile_text"],
                "candidate_id": self._candidate_id,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        scorecard_output["dimensions"] = resolved["dimensions"]

        # ------------------------------------------------------------------
        # 9. Persist (idempotent upsert).
        # ------------------------------------------------------------------
        self._current_phase = _PHASE_PERSIST
        # Stable, sorted dim names for the persisted column. Sorting
        # decouples downstream consumers from LLM-emission order so
        # diff-checks across runs stay clean.
        dimensions_rescored_sorted = sorted(set(self._dimensions_rescored))

        persist_result = await workflow.execute_activity(
            "scorecard.persist_scorecard",
            {
                "job_id": self._job_id,
                "candidate_id": self._candidate_id,
                "rubric_id": self._rubric_id,
                "submission_id": (
                    str(inp.submission_id) if inp.submission_id else None
                ),
                "overall_match_score": overall_score_str,
                "scorecard_output": scorecard_output,
                "self_correction_triggered": self._self_correction_triggered,
                "dimensions_rescored": dimensions_rescored_sorted,
                "tool_call_count": self._tool_call_count,
                "total_cost_usd": f"{self._total_cost_usd:.6f}",
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        self._current_phase = _PHASE_DONE

        workflow.logger.info(
            "ScorecardGeneratorWorkflow completed",
            extra={
                "job_id": self._job_id,
                "candidate_id": self._candidate_id,
                "scorecard_id": persist_result["scorecard_id"],
                "overall_match_score": overall_score_str,
                "self_correction_triggered": self._self_correction_triggered,
                "dimensions_rescored": dimensions_rescored_sorted,
                "tool_call_count": self._tool_call_count,
                "total_cost_usd": round(self._total_cost_usd, 6),
                "termination_reason": termination_reason,
            },
        )

        # Build the typed result. We construct via the Pydantic model so
        # `model_dump(mode="json")` handles UUID + Decimal serialization
        # uniformly — the worker / callers should never have to know the
        # Decimal bit-pattern of an in-flight overall_match_score.
        result = ScorecardWorkflowResult(
            scorecard_id=persist_result["scorecard_id"],
            overall_match_score=overall_score_str,  # type: ignore[arg-type]
            self_correction_triggered=self._self_correction_triggered,
            dimensions_rescored=dimensions_rescored_sorted,
            tool_call_count=self._tool_call_count,
            total_cost_usd=f"{self._total_cost_usd:.6f}",  # type: ignore[arg-type]
            termination_reason=termination_reason,  # type: ignore[arg-type]
        )
        return result.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Phase 1-2 helpers — DB context loaders.
    # ------------------------------------------------------------------

    async def _load_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Load the enriched candidate profile or raise ApplicationError.

        We translate ValueError-from-activity (candidate not found) into
        a non-retryable ApplicationError so Temporal surfaces it as a
        terminal failure instead of churning through the retry budget on
        a permanent error.
        """
        try:
            return await workflow.execute_activity(
                "scorecard.get_candidate_profile",
                {"candidate_id": candidate_id},
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=_DB_RETRY,
            )
        except Exception as exc:
            # ApplicationError with `non_retryable=True` is the documented
            # path to a terminal workflow failure with a structured
            # reason in Temporal's UI.
            raise ApplicationError(
                f"Candidate not loadable: {exc}",
                type="CandidateNotFound",
                non_retryable=True,
            ) from exc

    async def _load_job_rubric(
        self, job_id: str, rubric_id: str
    ) -> dict[str, Any]:
        """Load Job + Rubric or raise ApplicationError on precondition fail."""
        try:
            return await workflow.execute_activity(
                "scorecard.get_job_rubric",
                {"job_id": job_id, "rubric_id": rubric_id},
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=_DB_RETRY,
            )
        except Exception as exc:
            raise ApplicationError(
                f"Job/Rubric not loadable: {exc}",
                type="JobRubricNotFound",
                non_retryable=True,
            ) from exc

    # ------------------------------------------------------------------
    # Phase 6 — Reflective evidence-gathering loop.
    # ------------------------------------------------------------------

    async def _run_reflective_loop(
        self,
        *,
        Budget: Any,
        HistoryEntry: Any,
        append_history: Any,
        build_iteration_prompt: Any,
        render_tools_for_llm: Any,
        resolve_tool_call: Any,
        get_tool_timeout: Any,
        is_terminal_tool: Any,
        tool_specs: Any,
        candidate: dict[str, Any],
        job_rubric: dict[str, Any],
        scorecard_output: dict[str, Any],
        initial_gate: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Run the budgeted LLM-directed evidence-gathering loop.

        Returns the updated `scorecard_output` (with any rescored dims
        merged back into the dim list) plus the terminal `reason`
        string. The reason is one of `mark_scorecard_done`'s enum
        values; `budget_exhausted` is injected by this function (not
        the LLM) when the budget hits a hard ceiling.
        """
        # Scorecard-specific budget. Per plan §11 the per-candidate
        # budget is small enough that one ceiling per resource is
        # sufficient — we set soft warnings 25% below hard limits so
        # the Budget instance's logging surface stays useful but no
        # path actually depends on the soft warning to escalate.
        # `hard_tokens` is effectively unlimited because token counts
        # here are LLM-estimated, not provider-reported (see
        # llm_decision.py); enforcing a token budget on top of cost
        # would double-count the same resource.
        budget = Budget(
            soft_tool_calls=6,
            hard_tool_calls=8,
            soft_wallclock_seconds=150.0,
            hard_wallclock_seconds=180.0,
            soft_cost_usd=0.40,
            hard_cost_usd=0.50,
            soft_tokens=0,
            hard_tokens=999_999,
        )

        tool_catalog_md = render_tools_for_llm(tool_specs)

        # `history` carries the call ledger we feed back to the LLM on
        # each decision. Build a starter entry so the planner sees the
        # initial scoring + gate result as "step 0".
        history: list = []
        history = append_history(
            history,
            step=0,
            tool_name="initial_scoring",
            args={"dim_count": len(scorecard_output.get("dimensions", []) or [])},
            result_summary=(
                f"Scored {len(scorecard_output.get('dimensions', []) or [])} dims; "
                f"{initial_gate.get('low_conf_count', 0)} below confidence threshold."
            ),
            tokens_used=0,
            cost_usd=0.0,
            latency_ms=0,
        )

        # Track per-dim already-fetched / already-rescored bookkeeping
        # so the LLM doesn't waste budget refetching the same evidence
        # or rescoring already-confident dims. The LLM's prompt also
        # instructs it not to repeat — but defensive bookkeeping in
        # workflow code costs nothing and prevents pathological loops.
        low_conf_by_name: dict[str, dict] = {
            d["name"]: d for d in initial_gate.get("low_confidence_dimensions", []) or []
        }
        # Pending evidence keyed by dim name. When the LLM picks a
        # fetcher we stash the result; when it then picks
        # rescore_dimension we feed the stashed evidence verbatim.
        pending_evidence_by_dim: dict[str, dict] = {}
        # The most-recently-targeted dim (output of
        # select_evidence_source) is what the next fetcher / rescore
        # should attach to. Reset when the LLM picks a new dim.
        active_dim_name: str | None = None

        step = 1
        termination_reason: str | None = None

        while True:
            # -------------------------------------------------------------
            # Hard-budget gate: inject mark_scorecard_done("budget_exhausted").
            # We call the mark activity (rather than just breaking) so the
            # event history records the synthetic termination event the
            # same way it records an LLM-emitted one.
            # -------------------------------------------------------------
            if budget.should_terminate():
                workflow.logger.warning(
                    "Scorecard budget hard-exhausted; injecting mark_scorecard_done",
                    extra={
                        "job_id": self._job_id,
                        "candidate_id": self._candidate_id,
                        "tool_calls": budget._tool_calls,
                        "cost_usd": budget._cost_usd,
                        "elapsed_seconds": budget.elapsed_seconds,
                    },
                )
                await workflow.execute_activity(
                    "scorecard.mark_scorecard_done",
                    {
                        "reason": _REASON_BUDGET_EXHAUSTED,
                        "message": (
                            "Workflow injected termination — budget ceiling reached "
                            f"after {budget._tool_calls} tool calls."
                        ),
                    },
                    start_to_close_timeout=timedelta(seconds=5),
                    retry_policy=_DB_RETRY,
                )
                termination_reason = _REASON_BUDGET_EXHAUSTED
                break

            # -------------------------------------------------------------
            # Build the per-iteration prompt + ask the LLM what to do next.
            # -------------------------------------------------------------
            context = {
                "job_id": self._job_id,
                "candidate_id": self._candidate_id,
                "rubric_id": self._rubric_id,
                "candidate_summary": _candidate_summary_for_prompt(candidate),
                "github_username": candidate.get("github_username"),
                "scorecard_state": _summarize_scorecard_state(scorecard_output),
                "low_conf_remaining": sorted(low_conf_by_name.keys()),
                "active_dim_name": active_dim_name,
                "available_fetchers": _AVAILABLE_FETCHERS,
                "pending_evidence_dims": sorted(pending_evidence_by_dim.keys()),
            }

            llm_payload = build_iteration_prompt(
                goal_key="scorecard",
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

            tool_name: str = decision_raw.get("tool_name", "") or ""
            tool_args: dict = decision_raw.get("args", {}) or {}
            decision_tokens = int(decision_raw.get("tokens_used", 0) or 0)
            decision_cost = float(decision_raw.get("cost_usd", 0.0) or 0.0)

            # -------------------------------------------------------------
            # Terminal tool — LLM-decided termination. Persist the chosen
            # reason and break out of the loop.
            # -------------------------------------------------------------
            if is_terminal_tool(tool_name):
                # The LLM's `args` carry the actual reason. Validate it
                # against the closed enum here so a misbehaving model
                # cannot poison the persisted result with an unknown
                # value. Default to `diminishing_returns` if the LLM
                # somehow returned a reason outside the enum (the
                # mark_scorecard_done activity itself raises on
                # invalid enum, so this branch only protects against
                # missing-key cases).
                raw_reason = tool_args.get("reason")
                if raw_reason not in (
                    _REASON_ALL_DIMS_CONFIDENT,
                    _REASON_DIMINISHING_RETURNS,
                    _REASON_BUDGET_IMMINENT,
                ):
                    workflow.logger.warning(
                        "LLM emitted mark_scorecard_done with invalid reason; coercing",
                        extra={"returned_reason": raw_reason},
                    )
                    raw_reason = _REASON_DIMINISHING_RETURNS

                await workflow.execute_activity(
                    "scorecard.mark_scorecard_done",
                    {
                        "reason": raw_reason,
                        "message": tool_args.get("message"),
                    },
                    start_to_close_timeout=timedelta(seconds=5),
                    retry_policy=_DB_RETRY,
                )
                budget.charge(tokens=decision_tokens, cost_usd=decision_cost)
                self._tool_call_count += 1
                self._total_cost_usd += decision_cost
                self._total_tokens += decision_tokens

                termination_reason = raw_reason
                history = append_history(
                    history,
                    step=step,
                    tool_name=tool_name,
                    args={"reason": raw_reason},
                    result_summary=f"Loop terminated (reason={raw_reason})",
                    tokens_used=decision_tokens,
                    cost_usd=decision_cost,
                    latency_ms=int(decision_raw.get("latency_ms", 0) or 0),
                )
                break

            # -------------------------------------------------------------
            # Non-terminal tool — resolve via the core dispatcher and run.
            # -------------------------------------------------------------
            try:
                activity_name, validated_args = resolve_tool_call(
                    tool_name, tool_args
                )
            except (KeyError, ValueError) as exc:
                # Unknown tool name OR missing required args. Log,
                # append a synthetic history entry, charge the budget
                # for the decision call, and loop — the planner will
                # see the failure in history and (hopefully) recover.
                workflow.logger.warning(
                    "Scorecard planner emitted unresolvable tool call",
                    extra={
                        "tool_name": tool_name,
                        "error": str(exc)[:300],
                    },
                )
                budget.charge(tokens=decision_tokens, cost_usd=decision_cost)
                self._tool_call_count += 1
                self._total_cost_usd += decision_cost
                self._total_tokens += decision_tokens
                history = append_history(
                    history,
                    step=step,
                    tool_name=tool_name,
                    args=tool_args,
                    result_summary=f"tool_resolve_error: {str(exc)[:200]}",
                    tokens_used=decision_tokens,
                    cost_usd=decision_cost,
                    latency_ms=int(decision_raw.get("latency_ms", 0) or 0),
                )
                step += 1
                continue

            # -------------------------------------------------------------
            # Inject per-tool args the LLM cannot know about: the
            # candidate's github_username for fetchers, the
            # candidate_profile_summary for select_evidence_source /
            # rescore_dimension, the stashed evidence for
            # rescore_dimension, etc.
            # -------------------------------------------------------------
            validated_args = _augment_args(
                tool_name=tool_name,
                args=dict(validated_args),
                candidate=candidate,
                low_conf_by_name=low_conf_by_name,
                pending_evidence_by_dim=pending_evidence_by_dim,
                active_dim_name=active_dim_name,
                scorecard_output=scorecard_output,
            )

            retry_policy = (
                _LLM_RETRY if activity_name in _LLM_BACKED_ACTIVITIES else _DB_RETRY
            )

            result_raw: Any = await workflow.execute_activity(
                activity_name,
                validated_args,
                start_to_close_timeout=get_tool_timeout(activity_name),
                retry_policy=retry_policy,
            )

            if not isinstance(result_raw, dict):
                # Defensive: every scorecard activity returns a dict.
                # Drift here would silently break downstream dispatch.
                result_raw = {"value": result_raw}

            # -------------------------------------------------------------
            # Per-tool post-processing: track active dim, stash evidence,
            # merge rescored dim back into scorecard, update gate state.
            # -------------------------------------------------------------
            (
                active_dim_name,
                low_conf_by_name,
                scorecard_output,
                pending_evidence_by_dim,
                result_summary,
            ) = self._post_process_tool_result(
                tool_name=tool_name,
                activity_name=activity_name,
                args=validated_args,
                result=result_raw,
                active_dim_name=active_dim_name,
                low_conf_by_name=low_conf_by_name,
                scorecard_output=scorecard_output,
                pending_evidence_by_dim=pending_evidence_by_dim,
            )

            # -------------------------------------------------------------
            # Charge the budget. Tool cost = decision_cost +
            # activity-reported cost (if the activity surfaced one).
            # `score_candidate_dimensions` and `rescore_dimension` both
            # surface `cost_usd`; fetchers don't.
            # -------------------------------------------------------------
            activity_cost = float(result_raw.get("cost_usd", 0.0) or 0.0)
            activity_tokens = int(result_raw.get("tokens_used", 0) or 0)
            budget.charge(
                tokens=decision_tokens + activity_tokens,
                cost_usd=decision_cost + activity_cost,
            )
            self._tool_call_count += 1
            self._total_cost_usd += decision_cost + activity_cost
            self._total_tokens += decision_tokens + activity_tokens

            # -------------------------------------------------------------
            # If the gate now declares everything confident, the LLM
            # *should* terminate on the next iteration with
            # `all_dims_confident`. We don't short-circuit here — the
            # LLM is the decider per the agent spec (§5). The empty
            # `low_conf_by_name` will be visible in `context.low_conf_remaining`
            # so the LLM has the signal.
            # -------------------------------------------------------------
            history = append_history(
                history,
                step=step,
                tool_name=tool_name,
                args={k: str(v)[:100] for k, v in (tool_args or {}).items()},
                result_summary=result_summary,
                tokens_used=decision_tokens + activity_tokens,
                cost_usd=decision_cost + activity_cost,
                latency_ms=int(decision_raw.get("latency_ms", 0) or 0),
            )
            step += 1

        # Fallback: should never trigger because both break paths set
        # the reason, but a defensive default is cheaper than diagnosing
        # an UnboundLocalError later.
        if termination_reason is None:
            termination_reason = _REASON_DIMINISHING_RETURNS

        return scorecard_output, termination_reason

    # ------------------------------------------------------------------
    # Tool result post-processing.
    # ------------------------------------------------------------------

    def _post_process_tool_result(
        self,
        *,
        tool_name: str,
        activity_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        active_dim_name: str | None,
        low_conf_by_name: dict[str, dict],
        scorecard_output: dict[str, Any],
        pending_evidence_by_dim: dict[str, dict],
    ) -> tuple[str | None, dict[str, dict], dict[str, Any], dict[str, dict], str]:
        """Merge a tool's result into workflow state.

        Returns the updated `(active_dim_name, low_conf_by_name,
        scorecard_output, pending_evidence_by_dim, result_summary)`
        tuple. Every branch is deterministic on its inputs so replay
        is safe.
        """
        # -------------------- select_evidence_source --------------------
        if tool_name == "select_evidence_source":
            dim_obj = args.get("low_conf_dim") or {}
            dim_name = (
                dim_obj.get("name")
                if isinstance(dim_obj, dict)
                else None
            )
            picked = result.get("tool_name") or "skip"
            if picked == "skip":
                # No fetcher applies — drop the dim from the active set
                # so the LLM doesn't loop on it. The persisted dim keeps
                # its original (low) confidence; the operator UI labels
                # this as "evidence-limited" downstream.
                if dim_name and dim_name in low_conf_by_name:
                    low_conf_by_name.pop(dim_name, None)
                active_dim_name = None
                summary = f"select_evidence_source -> skip (dim={dim_name})"
            else:
                # The next fetcher call should target this dim.
                active_dim_name = dim_name if isinstance(dim_name, str) else None
                summary = (
                    f"select_evidence_source -> {picked} "
                    f"(dim={dim_name}; reasoning='{(result.get('reasoning') or '')[:80]}')"
                )
            return (
                active_dim_name,
                low_conf_by_name,
                scorecard_output,
                pending_evidence_by_dim,
                summary,
            )

        # -------------------- rescore_dimension -------------------------
        if tool_name == "rescore_dimension":
            dim_name = args.get("dimension_name") or active_dim_name
            new_score = result.get("new_score")
            new_confidence = result.get("new_confidence")
            new_rationale = result.get("new_rationale")
            new_citation_text = result.get("new_citation_text")

            if isinstance(dim_name, str):
                # Merge the updated values back into scorecard_output.
                # We rewrite the dim in place (by name) so dim ordering
                # stays stable across the loop (downstream
                # compute_overall_match_score is order-agnostic anyway,
                # but UI consumers may not be).
                dims = scorecard_output.get("dimensions", []) or []
                for i, d in enumerate(dims):
                    if not isinstance(d, dict):
                        continue
                    if d.get("name") == dim_name:
                        updated = dict(d)
                        if new_score is not None:
                            updated["score"] = new_score
                        if new_confidence is not None:
                            updated["confidence"] = new_confidence
                        if new_rationale:
                            updated["rationale"] = new_rationale
                        if new_citation_text:
                            # Replace the citation with the rescored
                            # text; resolve_citations will re-tier
                            # later. We strip char offsets here because
                            # the new text may not map into the old
                            # source positions.
                            updated["citation"] = {
                                "text": new_citation_text,
                                "resolution_method": "placeholder",
                            }
                        dims[i] = updated
                        break
                scorecard_output["dimensions"] = dims

                # Track the rescore for the persisted result.
                if dim_name not in self._dimensions_rescored:
                    self._dimensions_rescored.append(dim_name)

                # Update the gate: if confidence cleared the threshold,
                # drop from low_conf_by_name.
                from app.temporal.product.scorecard.activities.check_confidence_gate import (
                    CONFIDENCE_THRESHOLD,
                )

                if (
                    isinstance(new_confidence, (int, float))
                    and float(new_confidence) >= CONFIDENCE_THRESHOLD
                    and dim_name in low_conf_by_name
                ):
                    low_conf_by_name.pop(dim_name, None)

                # Clear the active dim + evidence stash for this dim.
                pending_evidence_by_dim.pop(dim_name, None)
                if active_dim_name == dim_name:
                    active_dim_name = None

            summary = (
                f"rescore_dimension dim={dim_name} score={new_score} "
                f"confidence={new_confidence}"
            )
            return (
                active_dim_name,
                low_conf_by_name,
                scorecard_output,
                pending_evidence_by_dim,
                summary,
            )

        # -------------------- fetchers ----------------------------------
        # Fetchers stash their result under the currently active dim so
        # the next rescore_dimension call can read it back via the
        # evidence injection in _augment_args.
        if activity_name in _FETCHER_ACTIVITIES:
            if active_dim_name:
                pending_evidence_by_dim[active_dim_name] = result
            count_hint = _result_count_hint(result)
            summary = (
                f"{tool_name} -> {count_hint} "
                f"(stashed for dim={active_dim_name or 'unassigned'})"
            )
            return (
                active_dim_name,
                low_conf_by_name,
                scorecard_output,
                pending_evidence_by_dim,
                summary,
            )

        # -------------------- fallback ----------------------------------
        return (
            active_dim_name,
            low_conf_by_name,
            scorecard_output,
            pending_evidence_by_dim,
            f"{tool_name} -> {str(result)[:120]}",
        )


# ---------------------------------------------------------------------------
# Module-level pure helpers (deterministic, replay-safe — no I/O, no time)
# ---------------------------------------------------------------------------


# The 5 MVP fetcher activity names. Used by the post-processing dispatch
# to know "this was a fetcher; stash the result". Kept as a frozenset so
# membership tests are O(1) and the set is immutable across runs.
_FETCHER_ACTIVITIES: frozenset[str] = frozenset(
    {
        "scorecard.fetch_repo_readmes",
        "scorecard.fetch_commit_history",
        "scorecard.fetch_pr_review_history",
        "scorecard.fetch_repo_languages",
        "scorecard.fetch_user_orgs_and_stars",
    }
)


# Tool *names* (what the LLM emits in tool_name) corresponding to the
# fetchers above — used by select_evidence_source to filter its
# allowlist. Order is stable for prompt-render determinism.
_AVAILABLE_FETCHERS: tuple[str, ...] = (
    "fetch_repo_readmes",
    "fetch_commit_history",
    "fetch_pr_review_history",
    "fetch_repo_languages",
    "fetch_user_orgs_and_stars",
)


def _candidate_summary_for_prompt(candidate: dict[str, Any]) -> str:
    """Build a compact one-paragraph summary fed into the planner prompts.

    Used by `select_evidence_source` and `rescore_dimension` via
    args injection. We deliberately stay under ~1000 chars so the
    summary fits comfortably in both Flash and Pro tier prompts
    without inflating cost.
    """
    enriched = candidate.get("enriched_data") or {}
    name = candidate.get("full_name") or "unknown"
    seniority = enriched.get("seniority") or "unknown"
    years = enriched.get("years_experience")
    years_str = str(years) if years is not None else "unknown"
    location = enriched.get("location") or "unknown"
    gh = candidate.get("github_username") or "(none)"
    skills_list = candidate.get("skills") or []
    skills_str = ", ".join(skills_list[:10]) if skills_list else "(none listed)"
    summary = (
        f"{name} — {seniority} engineer, {years_str} years experience, "
        f"based in {location}. GitHub: {gh}. Skills: {skills_str}."
    )
    return summary[:1000]


def _summarize_scorecard_state(scorecard_output: dict[str, Any]) -> dict[str, Any]:
    """Produce a compact JSON-friendly view of the current scorecard.

    Shape: `{dim_name: {score, confidence, evidence_limited}}` plus
    a `dim_count` field. Lives in the LLM's context window so the
    planner can reason about "which dims still need work".
    """
    dims = scorecard_output.get("dimensions", []) or []
    state: dict[str, dict[str, Any]] = {}
    for d in dims:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not isinstance(name, str):
            continue
        state[name] = {
            "score": d.get("score"),
            "confidence": d.get("confidence"),
            "evidence_limited": bool(d.get("evidence_limited", False)),
        }
    return {"dim_count": len(state), "dims": state}


def _result_count_hint(result: dict[str, Any]) -> str:
    """Render a compact 'returned N items' string for history summaries.

    Recognizes the schemas of all five fetchers; falls back to a
    truncated repr for unknown shapes. Pure, deterministic.
    """
    if not isinstance(result, dict):
        return str(result)[:100]
    for key in ("repos", "reviews", "organizations", "languages"):
        if key in result and isinstance(result[key], list):
            return f"{len(result[key])} {key}"
    if "total_count" in result:
        return f"total_count={result['total_count']}"
    if "total_commits_last_year" in result:
        return f"commits_last_year={result['total_commits_last_year']}"
    if "primary_language" in result:
        return f"primary_language={result['primary_language']}"
    return str(result)[:100]


def _augment_args(
    *,
    tool_name: str,
    args: dict[str, Any],
    candidate: dict[str, Any],
    low_conf_by_name: dict[str, dict],
    pending_evidence_by_dim: dict[str, dict],
    active_dim_name: str | None,
    scorecard_output: dict[str, Any],
) -> dict[str, Any]:
    """Inject workflow-controlled args the LLM cannot know about.

    The LLM picks the *tool* and the *args it can author* (dim name,
    keywords, reasons). Anything the workflow holds privately —
    github_username, candidate profile summary, the stashed fetcher
    evidence, the available_tools allowlist — gets stitched on here.
    Pure function of its inputs; deterministic.
    """
    github_username = candidate.get("github_username")
    candidate_summary = _candidate_summary_for_prompt(candidate)

    if tool_name == "select_evidence_source":
        # Allowlist narrows each iteration to fetchers that still make
        # sense for at least one low-conf dim. In v1 we keep it
        # fixed (5 MVP fetchers) so the planner has the full set; the
        # tighter narrowing is v2 work.
        if "available_tools" not in args:
            args["available_tools"] = list(_AVAILABLE_FETCHERS)
        if "candidate_profile_summary" not in args:
            args["candidate_profile_summary"] = candidate_summary
        if "github_username" not in args:
            args["github_username"] = github_username
        # If the LLM passed a low_conf_dim by *name only*, hydrate it
        # from the workflow's tracked low-conf set so the rest of the
        # spec (current_confidence / current_score) is present.
        dim_obj = args.get("low_conf_dim")
        if isinstance(dim_obj, dict) and "name" in dim_obj and "current_confidence" not in dim_obj:
            stored = low_conf_by_name.get(dim_obj["name"])
            if stored:
                args["low_conf_dim"] = stored

    elif tool_name in _AVAILABLE_FETCHERS:
        # All five fetchers require github_username; inject if absent.
        if "github_username" not in args or not args.get("github_username"):
            args["github_username"] = github_username

    elif tool_name == "rescore_dimension":
        dim_name = args.get("dimension_name") or active_dim_name
        if dim_name:
            args["dimension_name"] = dim_name

            # Original score / confidence / rationale / weight come
            # from the current scorecard_output (which may itself be a
            # prior rescore — that's intentional, we re-score over the
            # latest state).
            for dim in scorecard_output.get("dimensions", []) or []:
                if not isinstance(dim, dict):
                    continue
                if dim.get("name") == dim_name:
                    args.setdefault("original_score", int(dim.get("score", 0) or 0))
                    args.setdefault(
                        "original_confidence",
                        float(dim.get("confidence", 0.0) or 0.0),
                    )
                    args.setdefault(
                        "original_rationale",
                        str(dim.get("rationale", "") or ""),
                    )
                    args.setdefault(
                        "original_weight",
                        float(dim.get("weight", 0.0) or 0.0),
                    )
                    break

            # Evidence injection — the stashed fetcher output from the
            # most recent fetcher call targeting this dim. If the LLM
            # tries to rescore without a prior fetch, we pass an
            # empty dict — the rescore activity is well-defined on
            # empty evidence (it will likely keep confidence flat,
            # which is the correct outcome).
            if "evidence" not in args:
                args["evidence"] = pending_evidence_by_dim.get(dim_name, {})

            if "candidate_profile_summary" not in args:
                args["candidate_profile_summary"] = candidate_summary

    return args
