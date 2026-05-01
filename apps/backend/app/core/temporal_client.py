from temporalio.client import Client

from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_client: Client | None = None


async def get_temporal_client() -> Client:
    """Return process-wide Temporal client. Creates on first call."""
    global _client
    if _client is not None:
        return _client
    _client = await Client.connect(
        f"{settings.temporal.host}:{settings.temporal.port}",
        namespace=settings.temporal.namespace,
    )
    LOGGER.info(
        "Temporal client connected",
        extra={"host": settings.temporal.host, "namespace": settings.temporal.namespace},
    )
    return _client


async def close_temporal_client() -> None:
    """Close the process-wide Temporal client. Call from lifespan teardown."""
    global _client
    if _client is not None:
        # Temporal Python SDK client has no explicit close — just dereference
        _client = None
        LOGGER.info("Temporal client closed")
