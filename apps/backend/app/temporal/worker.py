import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)
from app.core.config import settings
from app.temporal.core.discovery import discover_all
from app.temporal.core.activity_registry import ActivityRegistry
from app.temporal.core.workflow_registry import WorkflowRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

discover_all()


async def run_workers():
    client = await Client.connect(
        f"{settings.temporal.host}:{settings.temporal.port}",
        namespace=settings.temporal.namespace,
    )

    all_activities = ActivityRegistry.get_all_activities()
    queues = WorkflowRegistry.get_workflows_by_queue()

    workers = []
    for queue_name, workflows in queues.items():
        worker = Worker(
            client,
            task_queue=queue_name,
            workflows=workflows,
            activities=list(all_activities.values()),
            max_concurrent_activities=10,
            max_concurrent_workflow_tasks=20,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_all_modules()
            ),
        )
        workers.append(worker.run())

    LOGGER.info(f"Starting {len(workers)} workers")
    await asyncio.gather(*workers)


if __name__ == "__main__":
    asyncio.run(run_workers())
