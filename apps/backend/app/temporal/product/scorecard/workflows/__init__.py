"""Scorecard Generator Agent (Agent 4) — workflows package.

Importing this package registers `ScorecardGeneratorWorkflow` with the
process-wide `WorkflowRegistry` on the `converio-queue` task queue. The
worker bootstrap imports `app.temporal.product.scorecard.workflows`
which in turn imports the workflow module — a single path is enough to
wire the workflow into the worker.
"""
from __future__ import annotations

from app.temporal.product.scorecard.workflows.scorecard_workflow import (  # noqa: F401
    ScorecardGeneratorWorkflow,
)

__all__ = ["ScorecardGeneratorWorkflow"]
