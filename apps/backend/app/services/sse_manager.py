import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID
from app.schemas.sse_schemas import SSEEvent, SSEEventType


class SSEManager:
    def __init__(self, poll_interval: float = 2.0):
        self.poll_interval = poll_interval

    async def stream_events(self, workflow_id: UUID) -> AsyncGenerator[str, None]:
        try:
            while True:
                yield self._format(SSEEvent(
                    event_type=SSEEventType.HEARTBEAT,
                    workflow_id=workflow_id,
                    data={"message": "keep-alive"},
                ))
                async for event in self._poll(workflow_id):
                    yield self._format(event)
                if await self._is_finished(workflow_id):
                    break
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            return

    async def _poll(self, workflow_id: UUID) -> AsyncGenerator[SSEEvent, None]:
        """Override in subclass to yield domain events."""
        return
        yield  # make it a generator

    async def _is_finished(self, workflow_id: UUID) -> bool:
        """Override in subclass to check completion."""
        return False

    def _format(self, event: SSEEvent) -> str:
        return f"event: {event.event_type.value}\ndata: {event.model_dump_json()}\n\n"
