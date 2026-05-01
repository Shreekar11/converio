"""RecruiterIndexingWorkflow — orchestrates the 6-step recruiter indexing pipeline.

Pipeline phases:
    1. resolve_recruiter_duplicates  — PG email lookup for canonical recruiter_id
    2. compute_placement_metrics     — derive fill_rate / avg_days / placements_by_stage
    3. generate_recruiter_embedding  — local sentence-transformers (no LLM)
    4. persist_recruiter_record      — upsert metrics + embedding onto existing row
    5. index_recruiter_to_graph      — Neo4j MERGE (Recruiter + Domain + CompanyStage)
    6. score_recruiter_credibility   — deterministic weighted score, finalize status

Wizard pre-creates the Recruiter + RecruiterClient + RecruiterPlacement rows
synchronously, so this workflow is enrichment-only — it never inserts the
recruiter row. The duplicate resolver returns the canonical id used by every
downstream activity.

A `get_status` query handler exposes phase, recruiter_id, and credibility_score
for live observability via Temporal queries.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.schemas.product.recruiter import (
        RecruiterIndexingInput,
        RecruiterIndexingResult,
    )
    from app.temporal.core.workflow_registry import WorkflowRegistry, WorkflowType
    from app.temporal.product.recruiter_indexing.activities.compute_placement_metrics import (
        compute_placement_metrics,
    )
    from app.temporal.product.recruiter_indexing.activities.generate_recruiter_embedding import (
        generate_recruiter_embedding,
    )
    from app.temporal.product.recruiter_indexing.activities.index_recruiter_to_graph import (
        index_recruiter_to_graph,
    )
    from app.temporal.product.recruiter_indexing.activities.persist_recruiter_record import (
        persist_recruiter_record,
    )
    from app.temporal.product.recruiter_indexing.activities.resolve_recruiter_duplicates import (
        resolve_recruiter_duplicates,
    )
    from app.temporal.product.recruiter_indexing.activities.score_recruiter_credibility import (
        score_recruiter_credibility,
    )

# Retry policies — recruiter pipeline has no LLM and no external API calls,
# so only DB and local-embedding policies are needed.
_DB_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=1.5)
_EMBED_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=1.5)


@WorkflowRegistry.register(category=WorkflowType.BUSINESS, task_queue="converio-queue")
@workflow.defn
class RecruiterIndexingWorkflow:
    """Orchestrates recruiter enrichment through 6 deterministic activities."""

    def __init__(self) -> None:
        self._phase: str = "initialized"
        self._recruiter_id: str | None = None
        self._credibility_score: float | None = None

    @workflow.query
    def get_status(self) -> dict:
        """Query handler — returns current orchestration state.

        Returns:
            dict with keys: phase, recruiter_id, credibility_score.
        """
        return {
            "phase": self._phase,
            "recruiter_id": self._recruiter_id,
            "credibility_score": self._credibility_score,
        }

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        """Execute the recruiter indexing pipeline.

        Args:
            input_data: JSON-serializable dict matching RecruiterIndexingInput.

        Returns:
            JSON-serializable dict matching RecruiterIndexingResult.
        """
        inp = RecruiterIndexingInput.model_validate(input_data)
        profile = inp.profile

        # Surface the recruiter_id from the wizard payload immediately so
        # `get_status` returns a useful value before the resolver runs.
        self._recruiter_id = profile.recruiter_id

        # Step 1: Resolve recruiter duplicates — confirm canonical id from PG.
        # Wizard pre-creates the row, so a missing match is a fail-fast condition
        # (the activity itself raises). The defensive check below is belt-and-suspenders.
        self._phase = "resolving_duplicates"
        dedup_result = await workflow.execute_activity(
            resolve_recruiter_duplicates,
            args=[profile.model_dump(mode="json")],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        existing_recruiter_id = dedup_result.get("existing_recruiter_id")
        if existing_recruiter_id is None:
            raise RuntimeError(
                "resolve_recruiter_duplicates returned no existing_recruiter_id; "
                "recruiter row must exist before indexing"
            )
        recruiter_id = existing_recruiter_id
        self._recruiter_id = recruiter_id

        # Step 2: Compute placement metrics from RecruiterPlacement rows.
        self._phase = "computing_metrics"
        metrics_data = await workflow.execute_activity(
            compute_placement_metrics,
            args=[recruiter_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # Step 3: Generate 384-dim embedding from profile text blob (local model).
        self._phase = "generating_embedding"
        embed_result = await workflow.execute_activity(
            generate_recruiter_embedding,
            args=[profile.model_dump(mode="json")],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_EMBED_RETRY,
        )
        embedding = embed_result["embedding"]

        # Step 4: Persist metrics + embedding onto the existing recruiter row.
        self._phase = "persisting"
        await workflow.execute_activity(
            persist_recruiter_record,
            args=[recruiter_id, embedding, metrics_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # Step 5: MERGE recruiter into Neo4j knowledge graph (idempotent).
        self._phase = "indexing_graph"
        await workflow.execute_activity(
            index_recruiter_to_graph,
            args=[recruiter_id, profile.model_dump(mode="json"), metrics_data],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # Step 6: Score credibility + finalize status (active vs pending).
        self._phase = "scoring_credibility"
        credibility_result = await workflow.execute_activity(
            score_recruiter_credibility,
            args=[recruiter_id, profile.model_dump(mode="json")],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        credibility_score = credibility_result["credibility_score"]
        status = credibility_result["status"]
        self._credibility_score = credibility_score

        self._phase = "completed"

        return RecruiterIndexingResult(
            recruiter_id=recruiter_id,
            status=status,
            credibility_score=credibility_score,
            source=inp.source,
        ).model_dump(mode="json")
