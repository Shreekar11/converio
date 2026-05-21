"""Scorecard Generator Agent (Agent 4) — import triggers activity/workflow registration.

Importing this package is enough to register every `scorecard.*` activity
with both the process-wide `ActivityRegistry` and Temporal's `@activity.defn`
registry, AND to register `ScorecardGeneratorWorkflow` with the
`WorkflowRegistry` on the `converio-queue` task queue.

The worker bootstrap (`app.temporal.worker`) calls
`app.temporal.core.discovery.discover_all()`, which recursively imports
every module under `app.temporal.product`. This package-level `__init__`
also imports activities and workflows explicitly so direct imports
(e.g. `import app.temporal.product.scorecard`) wire the agent into the
process even without the discovery walk.
"""
# Activities — side-effect-registers all 16 `scorecard.*` activities via
# `@activity.defn` + `ActivityRegistry.register("scorecard", ...)`.
from app.temporal.product.scorecard import activities  # noqa: F401

# Workflow — registers `ScorecardGeneratorWorkflow` with `WorkflowRegistry`
# on the `converio-queue` task queue via `@WorkflowRegistry.register(...)`.
from app.temporal.product.scorecard import workflows  # noqa: F401
