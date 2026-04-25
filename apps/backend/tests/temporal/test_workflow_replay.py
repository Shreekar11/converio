"""G3 — replay determinism test for CandidateIndexingWorkflow.

Replays a captured event history through the current workflow code to verify
no `NondeterminismError` is raised. This guards against accidental workflow
changes (e.g. reordered activity calls, new awaits, removed steps) that would
break running workflows during deploy.

How to (re-)generate the history fixture:

    1. Run the happy-path integration test once against the local Temporal
       dev server (or temporarily switch to a non-time-skipping environment),
       so a real history is persisted in Temporal.
    2. Export the history JSON:
            temporal workflow show \\
                --workflow-id test-wf-happy \\
                --output json \\
                > tests/fixtures/workflow_history_indexed.json
    3. Commit the fixture. The test will pick it up on the next run.

Until the fixture is captured, this test is skipped (not failed) so CI stays
green on a fresh checkout. Once the fixture exists, any change that breaks
replay determinism will fail this test.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.worker import Replayer

from app.temporal.product.candidate_indexing.workflows.candidate_indexing_workflow import (
    CandidateIndexingWorkflow,
)


_HISTORY_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "workflow_history_indexed.json"
)


async def test_workflow_replay_determinism() -> None:
    """Replay a recorded history — must not raise NondeterminismError."""
    if not _HISTORY_FIXTURE.exists():
        pytest.skip(
            f"workflow history fixture not present at {_HISTORY_FIXTURE} — "
            "run the happy-path workflow once and export its history (see "
            "module docstring for instructions)."
        )

    history_json = _HISTORY_FIXTURE.read_text()

    replayer = Replayer(workflows=[CandidateIndexingWorkflow])
    # Replayer.replay_workflow raises NondeterminismError if the recorded
    # history diverges from the current workflow definition. A clean return
    # means the workflow is replay-safe against this history.
    await replayer.replay_workflow(history_json)
