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

Step 6 and step 7 run sequentially (not parallel) so the Neo4j node is created
with the real candidate_id returned by the Postgres write — avoiding a "PENDING"
placeholder for new (non-duplicate) candidates.

A `get_status` query handler exposes phase, candidate_id, and completeness_score
for live observability via Temporal queries.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

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
        self._phase = "completed"

        return IndexingResult(
            candidate_id=candidate_id,
            status=completeness_result["status"],
            completeness_score=completeness_result["completeness_score"],
            was_duplicate=dedup_result["is_duplicate"],
            source=inp.source,
        ).model_dump(mode="json")
