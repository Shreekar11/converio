from datetime import datetime, timezone

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.database.models import WorkflowRun
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("shared", "record_workflow_run_start")
@activity.defn
async def record_workflow_run_start(
    workflow_id: str,
    workflow_type: str,
    job_id: str | None = None,
    candidate_id: str | None = None,
) -> None:
    """Insert a WorkflowRun row at workflow start for SSE/polling observability."""
    import uuid

    async with async_session_maker() as session:
        # Upsert — if replay or duplicate start, update rather than fail
        result = await session.execute(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.status = "running"
            existing.started_at = datetime.now(timezone.utc)
        else:
            run = WorkflowRun(
                workflow_id=workflow_id,
                workflow_type=workflow_type,
                job_id=uuid.UUID(job_id) if job_id else None,
                candidate_id=uuid.UUID(candidate_id) if candidate_id else None,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
        await session.commit()

    LOGGER.info(
        "WorkflowRun started",
        extra={"workflow_id": workflow_id, "workflow_type": workflow_type},
    )


@ActivityRegistry.register("shared", "record_workflow_run_complete")
@activity.defn
async def record_workflow_run_complete(
    workflow_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update WorkflowRun row at workflow completion."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        )
        run = result.scalar_one_or_none()
        if run:
            run.status = status
            run.completed_at = datetime.now(timezone.utc)
            if error:
                run.error = error
            await session.commit()
        else:
            LOGGER.warning(
                "WorkflowRun not found for completion update",
                extra={"workflow_id": workflow_id},
            )

    LOGGER.info(
        "WorkflowRun completed",
        extra={"workflow_id": workflow_id, "status": status},
    )
