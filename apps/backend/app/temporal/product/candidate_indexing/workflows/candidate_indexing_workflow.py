"""CandidateIndexingWorkflow — orchestrates the 8-step candidate indexing pipeline.

Pipeline phases:
    1. parse_resume                — docling + LLM extraction
    2. fetch_github_signals        — external GitHub API
    3. infer_skill_depth           — re-tag skills using GitHub evidence
    4. resolve_entity_duplicates   — PG + Neo4j read for dedup
    5. generate_embedding          — vector embedding for similarity search
    6. persist_candidate_record    — write to Postgres (yields candidate_id)
    7. index_candidate_to_graph    — write to Neo4j with real candidate_id
    8. score_profile_completeness  — compute completeness + finalize status
    9. (optional) spawn ScorecardGeneratorWorkflow as a fire-and-forget
       child when `rubric_id` + `job_id` were supplied on input.
       See plan §16 Phase 7 / §18.1 for the trigger-location rationale —
       spawning here (not from JobIntakeWorkflow) keeps fan-in pressure
       off the root workflow's event loop.

Step 6 and step 7 run sequentially (not parallel) so the Neo4j node is created
with the real candidate_id returned by the Postgres write — avoiding a "PENDING"
placeholder for new (non-duplicate) candidates.

A `get_status` query handler exposes phase, candidate_id, and completeness_score
for live observability via Temporal queries.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from app.schemas.product.candidate import CandidateIndexingInput, IndexingResult
    from app.temporal.core.workflow_registry import WorkflowRegistry, WorkflowType
    from app.temporal.product.candidate_indexing.activities.fetch_github_signals import (
        fetch_github_signals,
    )
    from app.temporal.product.candidate_indexing.activities.generate_embedding import (
        generate_embedding,
    )
    from app.temporal.product.candidate_indexing.activities.index_candidate_to_graph import (
        index_candidate_to_graph,
    )
    from app.temporal.product.candidate_indexing.activities.infer_skill_depth import (
        infer_skill_depth,
    )
    from app.temporal.product.candidate_indexing.activities.parse_resume import parse_resume
    from app.temporal.product.candidate_indexing.activities.persist_candidate_record import (
        persist_candidate_record,
    )
    from app.temporal.product.candidate_indexing.activities.resolve_entity_duplicates import (
        resolve_entity_duplicates,
    )
    from app.temporal.product.candidate_indexing.activities.score_profile_completeness import (
        score_profile_completeness,
    )

# Retry policies per Q6 of implementation plan
_LLM_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2.0)
_GITHUB_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=120),
)
_DB_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=1.5)
_EMBED_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=1.5)


@WorkflowRegistry.register(category=WorkflowType.BUSINESS, task_queue="converio-queue")
@workflow.defn
class CandidateIndexingWorkflow:
    """Orchestrates candidate resume ingestion through 8 activities."""

    def __init__(self) -> None:
        self._phase: str = "initialized"
        self._candidate_id: str | None = None
        self._completeness_score: float | None = None

    @workflow.query
    def get_status(self) -> dict:
        """Query handler — returns current orchestration state.

        Returns:
            dict with keys: phase, candidate_id, completeness_score.
        """
        return {
            "phase": self._phase,
            "candidate_id": self._candidate_id,
            "completeness_score": self._completeness_score,
        }

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        """Execute the candidate indexing pipeline.

        Args:
            input_data: JSON-serializable dict matching CandidateIndexingInput.

        Returns:
            JSON-serializable dict matching IndexingResult.
        """
        inp = CandidateIndexingInput.model_validate(input_data)

        # Step 1: Obtain structured candidate profile.
        # - resume_file: parse file bytes via docling + LLM.
        # - profile: seed fast path, parsing is skipped.
        if inp.input_kind == "resume_file":
            self._phase = "parsing_resume"
            profile_data = await workflow.execute_activity(
                parse_resume,
                args=[inp.resume_file.model_dump(mode="json")],
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=_LLM_RETRY,
            )
        else:
            self._phase = "profile_provided"
            profile_data = inp.profile.model_dump(mode="json")

        # Step 2: Fetch GitHub signals (external API, high-retry)
        self._phase = "fetching_github"
        github_username = profile_data.get("github_username")
        github_signals_data = await workflow.execute_activity(
            fetch_github_signals,
            args=[github_username],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_GITHUB_RETRY,
        )

        # Step 3: Infer skill depth using GitHub evidence
        self._phase = "inferring_skill_depth"
        profile_data = await workflow.execute_activity(
            infer_skill_depth,
            args=[profile_data, github_signals_data],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_LLM_RETRY,
        )

        # Step 4: Resolve entity duplicates (PG + Neo4j read)
        self._phase = "resolving_duplicates"
        dedup_result = await workflow.execute_activity(
            resolve_entity_duplicates,
            args=[profile_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        existing_candidate_id = dedup_result.get("existing_candidate_id")

        # Step 5: Generate embedding
        self._phase = "generating_embedding"
        embed_result = await workflow.execute_activity(
            generate_embedding,
            args=[profile_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_EMBED_RETRY,
        )
        embedding = embed_result["embedding"]

        # Step 6: Persist PG record first to obtain real candidate_id.
        # Sequenced before graph indexing so Neo4j node uses the real id (not "PENDING").
        self._phase = "persisting"
        persist_result = await workflow.execute_activity(
            persist_candidate_record,
            args=[
                profile_data,
                embedding,
                github_signals_data,
                inp.source,
                inp.source_recruiter_id,
                existing_candidate_id,
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        candidate_id = persist_result["candidate_id"]
        self._candidate_id = candidate_id

        # Step 7: Index candidate to Neo4j graph with the real candidate_id.
        self._phase = "indexing_graph"
        await workflow.execute_activity(
            index_candidate_to_graph,
            args=[candidate_id, profile_data, github_signals_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # Step 8: Score completeness + update status
        self._phase = "scoring_completeness"
        completeness_result = await workflow.execute_activity(
            score_profile_completeness,
            args=[candidate_id, profile_data, github_signals_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        self._completeness_score = completeness_result["completeness_score"]
        indexing_status = completeness_result["status"]

        # Phase 9 — Trigger downstream scorecard generation.
        # Spawn ScorecardGeneratorWorkflow as a DETACHED child (fire-and-forget)
        # so this workflow returns immediately without blocking on the
        # scorecard's reflective loop (plan §16 Phase 7, §18.1).
        #
        # Skip the spawn when:
        #   1. indexing produced `failed` status (no candidate worth scoring),
        #   2. rubric_id was not supplied on input (no job context),
        #   3. job_id was not supplied on input (cannot pin scorecard to a job).
        #
        # The DB upsert in `scorecard.persist_scorecard` is the authoritative
        # dedup point; the rubric-id-suffixed child workflow ID below merely
        # guards against same-rubric replay producing duplicate workflow runs.
        if (
            indexing_status != "failed"
            and inp.rubric_id is not None
            and inp.job_id is not None
        ):
            await self._spawn_scorecard_child(
                job_id=str(inp.job_id),
                candidate_id=candidate_id,
                rubric_id=str(inp.rubric_id),
                submission_id=(
                    str(inp.submission_id) if inp.submission_id else None
                ),
            )
        else:
            # Graceful skip path — log enough context for ops to diagnose
            # why a scorecard never appeared for a successfully-indexed
            # candidate without polluting the happy-path with a warning.
            workflow.logger.info(
                "Skipping ScorecardGeneratorWorkflow spawn",
                extra={
                    "candidate_id": candidate_id,
                    "indexing_status": indexing_status,
                    "job_id": str(inp.job_id) if inp.job_id else None,
                    "rubric_id": str(inp.rubric_id) if inp.rubric_id else None,
                    "reason": (
                        "indexing_failed" if indexing_status == "failed"
                        else "missing_job_id" if inp.job_id is None
                        else "missing_rubric_id"
                    ),
                },
            )

        self._phase = "completed"

        return IndexingResult(
            candidate_id=candidate_id,
            status=indexing_status,
            completeness_score=completeness_result["completeness_score"],
            was_duplicate=dedup_result["is_duplicate"],
            source=inp.source,
        ).model_dump(mode="json")

    async def _spawn_scorecard_child(
        self,
        *,
        job_id: str,
        candidate_id: str,
        rubric_id: str,
        submission_id: str | None,
    ) -> None:
        """Spawn the scorecard child workflow as fire-and-forget.

        Uses `workflow.start_child_workflow` (NOT `execute_child_workflow`)
        so the parent (`CandidateIndexingWorkflow`) returns immediately —
        the scorecard's budgeted reflective loop runs independently and is
        observable via Temporal's UI on its own workflow ID.

        Workflow ID format: `scorecard-{job_id}-{candidate_id}-{rubric_prefix}`.
        The rubric prefix is intentional: rubric updates (HITL #2 re-eval)
        bump the rubric_id, which yields a fresh workflow ID and a fresh
        Scorecard row at the persistence layer. With
        `REJECT_DUPLICATE`, a same-rubric replay (e.g. CandidateIndexing
        re-runs after a non-deterministic worker restart) is safely
        deduplicated by Temporal itself.

        Failure isolation: we catch and log any spawn-time error so
        indexing's own success is never blocked by scorecard wiring
        problems. The scorecard can always be retried out-of-band
        via the operator API once the underlying issue is resolved.
        """
        # Rubric-id prefix keeps the workflow ID readable in the Temporal
        # UI while still differentiating across rubric versions. 8 chars
        # of a UUID are sufficient — Temporal IDs are scoped per
        # namespace and the (job_id, candidate_id) pair already
        # uniquifies the run.
        rubric_prefix = rubric_id.replace("-", "")[:8]
        child_workflow_id = f"scorecard-{job_id}-{candidate_id}-{rubric_prefix}"

        with workflow.unsafe.imports_passed_through():
            from app.schemas.product.scorecard import ScorecardWorkflowInput

        scorecard_input = ScorecardWorkflowInput(
            job_id=job_id,  # type: ignore[arg-type]
            candidate_id=candidate_id,  # type: ignore[arg-type]
            rubric_id=rubric_id,  # type: ignore[arg-type]
            submission_id=submission_id,  # type: ignore[arg-type]
        ).model_dump(mode="json")

        try:
            # `start_child_workflow` returns a handle once the child is
            # *scheduled* — we deliberately do NOT await the handle's
            # result (that would block on full execution).
            await workflow.start_child_workflow(
                "ScorecardGeneratorWorkflow",
                scorecard_input,
                id=child_workflow_id,
                task_queue="converio-queue",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            workflow.logger.info(
                "ScorecardGeneratorWorkflow spawned",
                extra={
                    "child_workflow_id": child_workflow_id,
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "rubric_id": rubric_id,
                    "submission_id": submission_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            # Two expected failure modes:
            #   * WorkflowAlreadyStartedError — same (job, candidate, rubric)
            #     replay; safe to swallow because the existing run is the
            #     source of truth.
            #   * Transient Temporal-frontend errors — out of scope to retry
            #     here; downstream operator endpoint can re-spawn.
            # We log at WARNING (not ERROR) because indexing itself succeeded.
            workflow.logger.warning(
                "Failed to spawn ScorecardGeneratorWorkflow; indexing still succeeded",
                extra={
                    "child_workflow_id": child_workflow_id,
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "rubric_id": rubric_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                },
            )
