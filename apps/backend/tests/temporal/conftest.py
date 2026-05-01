"""Fixtures for CandidateIndexingWorkflow integration tests.

These tests run the workflow in a Temporal time-skipping environment with
in-process activity stubs — no Postgres, Neo4j, or external HTTP calls.

Stub activities are registered under the same names as the production
activities (via `@activity.defn` defaulting to the function's `__name__`)
so the workflow's `workflow.execute_activity(...)` calls resolve to them.
"""
from __future__ import annotations

import pytest

from app.schemas.product.candidate import (
    CandidateProfile,
    GitHubSignals,
    Skill,
    WorkHistoryItem,
)


# ---------- Profile fixtures ----------

GOOD_PROFILE = CandidateProfile(
    full_name="Test Candidate",
    email="test@example.com",
    seniority="senior",
    years_experience=6,
    location="San Francisco",
    github_username="testcandidate",
    stage_fit=["series_a", "series_b"],
    skills=[
        Skill(name="Python"),
        Skill(name="FastAPI"),
        Skill(name="PostgreSQL"),
    ],
    work_history=[
        WorkHistoryItem(
            company="Stripe",
            role_title="Senior Engineer",
            start_date="2020",
            end_date="2023",
        )
    ],
    resume_text="Experienced engineer with 6 years building distributed systems.",
)

GOOD_GITHUB = GitHubSignals(
    repo_count=15,
    top_language="Python",
    commits_12m=250,
    stars_total=40,
    languages={"Python": 10, "TypeScript": 3, "Go": 2},
)

# Minimal profile — completeness score will fall below 0.5 -> review_queue.
SPARSE_PROFILE = CandidateProfile(full_name="Sparse Candidate")

SPARSE_GITHUB = GitHubSignals()


# ---------- Constants reused across tests ----------

CANDIDATE_ID = "11111111-1111-1111-1111-111111111111"
EXISTING_CANDIDATE_ID = "22222222-2222-2222-2222-222222222222"
TEST_TASK_QUEUE = "converio-test-queue"


@pytest.fixture
def good_profile_dict() -> dict:
    return GOOD_PROFILE.model_dump(mode="json")


@pytest.fixture
def good_github_dict() -> dict:
    return GOOD_GITHUB.model_dump(mode="json")


@pytest.fixture
def sparse_profile_dict() -> dict:
    return SPARSE_PROFILE.model_dump(mode="json")


@pytest.fixture
def sparse_github_dict() -> dict:
    return SPARSE_GITHUB.model_dump(mode="json")
