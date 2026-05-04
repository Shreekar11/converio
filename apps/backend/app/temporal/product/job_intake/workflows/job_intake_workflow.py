"""JobIntakeWorkflow — root workflow orchestrating the 3-step job intake pipeline.

Pipeline phases:
    1. classify_role_type           — LLM extracts RoleClassification from JD
    2. generate_evaluation_rubric   — LLM produces a 4-8 dimension weighted rubric
    3. persist_job_record           — single-tx UPDATE Job + INSERT Rubric v1 + UPSERT WorkflowRun

The intake API pre-creates the `Job` row (status="intake") and starts the workflow
fire-and-forget with `WorkflowIDReusePolicy.REJECT_DUPLICATE` (D3). On success,
`Job.status` transitions to `recruiter_assignment` — the terminal state for this
PR (D9). The downstream `RecruiterAssignmentWorkflow` (Agent 0) will be invoked
as a child workflow in a follow-up PR; the call site is stubbed below.

Activities registered under dotted names (`job_intake.<activity>`) so the
workflow invokes them by string rather than function reference — keeps the
sandbox import boundary thin.

A `current_phase` query handler exposes the orchestration phase for live
observability via Temporal queries (used by the future SSE endpoint).
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.schemas.enums import JobStatus
    from app.schemas.product.job import JobIntakeInput, JobIntakeResult
    from app.temporal.core.workflow_registry import WorkflowRegistry, WorkflowType

# Retry policies — mirror the indexing workflows' shape (module-level constants).
# LLM activities (classify, rubric) get exponential backoff with up to 3 attempts;
# DB activities (persist) get faster backoff with a tighter ceiling.
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


@WorkflowRegistry.register(category=WorkflowType.BUSINESS, task_queue="converio-queue")
@workflow.defn(name="JobIntakeWorkflow")
class JobIntakeWorkflow:
    """Orchestrates job intake through 3 deterministic activities."""

    def __init__(self) -> None:
        self._current_phase: str = "initialized"
        self._job_id: str | None = None
        self._rubric_version: int | None = None

    @workflow.query(name="current_phase")
    def current_phase(self) -> str:
        """Query handler — returns current orchestration phase string."""
        return self._current_phase

    @workflow.query
    def get_status(self) -> dict:
        """Query handler — returns current orchestration state.

        Returns:
            dict with keys: phase, job_id, rubric_version.
        """
        return {
            "phase": self._current_phase,
            "job_id": self._job_id,
            "rubric_version": self._rubric_version,
        }

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        """Execute the job intake pipeline.

        Args:
            input_data: JSON-serializable dict matching JobIntakeInput.

        Returns:
            JSON-serializable dict matching JobIntakeResult.
        """
        inp = JobIntakeInput.model_validate(input_data)
        self._job_id = inp.job_id

        workflow.logger.info(
            "JobIntakeWorkflow starting",
            extra={"job_id": inp.job_id, "title_len": len(inp.title)},
        )

        # Phase 1: Classify role — LLM extracts RoleClassification.
        self._current_phase = "classify_role_type"
        classification = await workflow.execute_activity(
            "job_intake.classify_role_type",
            {
                "title": inp.title,
                "jd_text": inp.jd_text,
                "intake_notes": inp.intake_notes,
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_LLM_RETRY,
        )

        # Phase 2: Generate evaluation rubric — LLM produces weighted dimensions.
        # `intake_notes` feeds both LLMs (D8) — same content, two prompts.
        self._current_phase = "generate_evaluation_rubric"
        rubric = await workflow.execute_activity(
            "job_intake.generate_evaluation_rubric",
            {
                "classification": classification,
                "intake_notes": inp.intake_notes,
                "extra": inp.extra,
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_LLM_RETRY,
        )

        # Phase 3: Persist — single-tx Job UPDATE + Rubric v1 INSERT + WorkflowRun upsert.
        self._current_phase = "persist_job_record"
        persisted = await workflow.execute_activity(
            "job_intake.persist_job_record",
            {
                "job_id": inp.job_id,
                "classification": classification,
                "rubric": rubric,
                "workflow_id": workflow.info().workflow_id,
            },
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_DB_RETRY,
        )
        self._rubric_version = persisted["rubric_version"]

        # Phase 4 (stub) — Agent 0 RecruiterAssignmentWorkflow.
        # TODO(agent-0): replace with `await workflow.execute_child_workflow(
        #     "RecruiterAssignmentWorkflow",
        #     {"job_id": inp.job_id, "classification": classification},
        #     id=f"recruiter-assignment-{inp.job_id}",
        #     task_queue="converio-queue",
        # )` once that workflow ships. Until then the parent exits with
        # status=recruiter_assignment so the persistence contract stays stable
        # for the Agent 0 PR to plug into without changes here.

        self._current_phase = "completed"

        workflow.logger.info(
            "JobIntakeWorkflow completed",
            extra={
                "job_id": persisted["job_id"],
                "rubric_id": persisted["rubric_id"],
                "rubric_version": persisted["rubric_version"],
            },
        )

        return JobIntakeResult(
            job_id=persisted["job_id"],
            rubric_id=persisted["rubric_id"],
            rubric_version=persisted["rubric_version"],
            status=JobStatus.RECRUITER_ASSIGNMENT,
        ).model_dump(mode="json")
