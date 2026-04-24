from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Type


class WorkflowType(str, Enum):
    SHARED = "shared"
    BUSINESS = "business"


@dataclass
class WorkflowMetadata:
    workflow_class: Type
    name: str
    category: WorkflowType
    task_queue: str


class WorkflowRegistry:
    _workflows: Dict[str, WorkflowMetadata] = {}

    @classmethod
    def register(cls, category: WorkflowType, task_queue: str = "converio-queue"):
        def decorator(workflow_class):
            cls._workflows[workflow_class.__name__] = WorkflowMetadata(
                workflow_class=workflow_class,
                name=workflow_class.__name__,
                category=category,
                task_queue=task_queue,
            )
            return workflow_class
        return decorator

    @classmethod
    def get_workflows_by_queue(cls) -> Dict[str, List[Type]]:
        result = {}
        for meta in cls._workflows.values():
            result.setdefault(meta.task_queue, []).append(meta.workflow_class)
        return result
