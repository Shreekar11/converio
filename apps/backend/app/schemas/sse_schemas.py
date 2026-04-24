from datetime import datetime
from enum import Enum
from typing import Dict, Optional
from uuid import UUID
from pydantic import BaseModel


class SSEEventType(str, Enum):
    WORKFLOW_STARTED = "workflow:started"
    WORKFLOW_PROGRESS = "workflow:progress"
    WORKFLOW_COMPLETED = "workflow:completed"
    WORKFLOW_FAILED = "workflow:failed"
    STAGE_STARTED = "stage:started"
    STAGE_COMPLETED = "stage:completed"
    STAGE_FAILED = "stage:failed"
    COMPARISON_COMPLETED = "comparison:completed"
    HEARTBEAT = "heartbeat"


class SSEEvent(BaseModel):
    event_type: SSEEventType
    workflow_id: UUID
    timestamp: datetime = datetime.utcnow()
    data: Dict
