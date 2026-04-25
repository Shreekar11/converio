"""Tests for C2 activity: infer_skill_depth."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.llm.base import LLMResponse
from app.schemas.product.candidate import (
    CandidateProfile,
    GitHubSignals,
    Skill,
    WorkHistoryItem,
)
from app.temporal.product.candidate_indexing.activities.infer_skill_depth import (
    infer_skill_depth,
)

_ACTIVITY_MODULE = (
    "app.temporal.product.candidate_indexing.activities.infer_skill_depth"
)


def _base_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="Jane Doe",
        seniority="senior",
        skills=[
            Skill(name="Python", depth="claimed_only"),
            Skill(name="TypeScript", depth="claimed_only"),
            Skill(name="Rust", depth="claimed_only"),
        ],
        work_history=[
            WorkHistoryItem(company="Stripe", role_title="Senior Engineer")
        ],
    )


def _llm_response(payload: object) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload),
        model="test-model",
        provider="fake",
        raw=None,
    )


async def test_no_github_returns_unchanged() -> None:
    """Empty GitHub signals -> profile returned unchanged, all skills claimed_only."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")
    empty_github = GitHubSignals().model_dump(mode="json")

    # No LLM should be called — but patch defensively to ensure no real client is built.
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock()

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, empty_github)

    assert [s["name"] for s in result["skills"]] == ["Python", "TypeScript", "Rust"]
    assert all(s["depth"] == "claimed_only" for s in result["skills"])
    mock_llm.complete.assert_not_awaited()


async def test_no_github_when_signals_dict_is_none() -> None:
    """github_signals_data falsy -> early-return path, no LLM call."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock()

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, {})

    assert all(s["depth"] == "claimed_only" for s in result["skills"])
    mock_llm.complete.assert_not_awaited()


async def test_skill_depth_updated_via_llm() -> None:
    """LLM returns matching-count skill array with new depths -> profile updates."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")
    github = GitHubSignals(
        repo_count=10,
        top_language="Python",
        commits_12m=250,
        languages={"Python": 6, "TypeScript": 3},
    ).model_dump(mode="json")

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=_llm_response(
            [
                {"name": "Python", "depth": "evidenced_commits"},
                {"name": "TypeScript", "depth": "evidenced_projects"},
                {"name": "Rust", "depth": "claimed_only"},
            ]
        )
    )

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, github)

    by_name = {s["name"]: s["depth"] for s in result["skills"]}
    assert by_name == {
        "Python": "evidenced_commits",
        "TypeScript": "evidenced_projects",
        "Rust": "claimed_only",
    }
    mock_llm.complete.assert_awaited_once()


async def test_skill_count_mismatch_falls_back_to_original() -> None:
    """LLM returns fewer skills than input -> activity must fall back, not crash."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")
    github = GitHubSignals(
        repo_count=5,
        languages={"Python": 3},
        commits_12m=50,
    ).model_dump(mode="json")

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=_llm_response(
            [
                {"name": "Python", "depth": "evidenced_projects"},
            ]
        )
    )

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, github)

    assert len(result["skills"]) == 3
    assert all(s["depth"] == "claimed_only" for s in result["skills"])


async def test_malformed_llm_response_falls_back() -> None:
    """LLM returns non-JSON garbage -> activity must fall back to original skills."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")
    github = GitHubSignals(
        repo_count=5,
        languages={"Python": 3},
        commits_12m=50,
    ).model_dump(mode="json")

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=LLMResponse(
            content="not json at all {{{",
            model="test-model",
            provider="fake",
        )
    )

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, github)

    assert len(result["skills"]) == 3
    assert all(s["depth"] == "claimed_only" for s in result["skills"])


async def test_llm_response_with_markdown_fences_is_parsed() -> None:
    """LLM response wrapped in ```json ... ``` fences should still parse."""
    profile = _base_profile()
    profile_data = profile.model_dump(mode="json")
    github = GitHubSignals(
        repo_count=10,
        languages={"Python": 6, "TypeScript": 3},
        commits_12m=250,
    ).model_dump(mode="json")

    fenced_payload = (
        "```json\n"
        + json.dumps(
            [
                {"name": "Python", "depth": "evidenced_commits"},
                {"name": "TypeScript", "depth": "evidenced_projects"},
                {"name": "Rust", "depth": "claimed_only"},
            ]
        )
        + "\n```"
    )

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=LLMResponse(
            content=fenced_payload,
            model="test-model",
            provider="fake",
        )
    )

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await infer_skill_depth(profile_data, github)

    by_name = {s["name"]: s["depth"] for s in result["skills"]}
    assert by_name["Python"] == "evidenced_commits"
    assert by_name["TypeScript"] == "evidenced_projects"
    assert by_name["Rust"] == "claimed_only"
