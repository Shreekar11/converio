"""Replay determinism tests for indexing workflows.

Replays captured event histories through the current workflow code to verify
no `NondeterminismError` is raised. This guards against accidental workflow
changes (e.g. reordered activity calls, new awaits, removed steps) that would
break running workflows during deploy.

How to (re-)generate a history fixture:

    1. Run the happy-path integration test once against the local Temporal
       dev server (or temporarily switch to a non-time-skipping environment),
       so a real history is persisted in Temporal.
    2. Export the history JSON for the relevant workflow id:
            temporal workflow show \\
                --workflow-id test-wf-happy \\
                --output json \\
                > tests/fixtures/workflow_history_indexed.json
            temporal workflow show \\
                --workflow-id test-recruiter-wf-happy \\
                --output json \\
                > tests/fixtures/workflow_history_recruiter_indexed.json
    3. Commit the fixture. The test will pick it up on the next run.

Until the fixture is captured, the corresponding test is skipped (not failed)
so CI stays green on a fresh checkout. Once the fixture exists, any change
that breaks replay determinism will fail the test.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.worker import Replayer

from app.temporal.product.candidate_indexing.workflows.candidate_indexing_workflow import (
    CandidateIndexingWorkflow,
)
from app.temporal.product.recruiter_indexing.workflows.recruiter_indexing_workflow import (
    RecruiterIndexingWorkflow,
)


_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_CANDIDATE_HISTORY_FIXTURE = _FIXTURES_DIR / "workflow_history_indexed.json"
_RECRUITER_HISTORY_FIXTURE = _FIXTURES_DIR / "workflow_history_recruiter_indexed.json"


async def test_workflow_replay_determinism() -> None:
    """Replay a recorded CandidateIndexingWorkflow history — no NondeterminismError."""
    if not _CANDIDATE_HISTORY_FIXTURE.exists():
        pytest.skip(
            f"candidate workflow history fixture not present at {_CANDIDATE_HISTORY_FIXTURE} "
            "— run the happy-path workflow once and export its history (see module docstring)."
        )

    history_json = _CANDIDATE_HISTORY_FIXTURE.read_text()

    replayer = Replayer(workflows=[CandidateIndexingWorkflow])
    # Replayer.replay_workflow raises NondeterminismError if the recorded
    # history diverges from the current workflow definition. A clean return
    # means the workflow is replay-safe against this history.
    await replayer.replay_workflow(history_json)


async def test_recruiter_indexing_workflow_replay_determinism() -> None:
    """Replay a recorded RecruiterIndexingWorkflow history — no NondeterminismError."""
    if not _RECRUITER_HISTORY_FIXTURE.exists():
        pytest.skip(
            f"recruiter workflow history fixture not present at {_RECRUITER_HISTORY_FIXTURE} "
            "— run the happy-path workflow once and export its history (see module docstring)."
        )

    history_json = _RECRUITER_HISTORY_FIXTURE.read_text()

    replayer = Replayer(workflows=[RecruiterIndexingWorkflow])
    await replayer.replay_workflow(history_json)
