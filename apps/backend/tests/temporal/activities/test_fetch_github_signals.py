"""Tests for D1 activity: fetch_github_signals."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.github_client import GitHubNotFound, GitHubRateLimited
from app.core.github_client import GitHubSignals as ClientGitHubSignals
from app.temporal.product.candidate_indexing.activities.fetch_github_signals import (
    fetch_github_signals,
)


_ACTIVITY_MODULE = (
    "app.temporal.product.candidate_indexing.activities.fetch_github_signals"
)


async def test_none_username_returns_empty() -> None:
    result = await fetch_github_signals(None)

    assert result["repo_count"] == 0
    assert result["top_language"] is None
    assert result["commits_12m"] == 0
    assert result["stars_total"] == 0
    assert result["languages"] == {}


async def test_empty_string_username_returns_empty() -> None:
    result = await fetch_github_signals("")

    assert result["repo_count"] == 0
    assert result["languages"] == {}


async def test_not_found_returns_empty() -> None:
    fake_client = AsyncMock()
    fake_client.fetch_user_signals = AsyncMock(
        side_effect=GitHubNotFound("user not found")
    )

    with patch(f"{_ACTIVITY_MODULE}.get_github_client", return_value=fake_client):
        result = await fetch_github_signals("ghost-user")

    assert result["repo_count"] == 0
    assert result["top_language"] is None
    assert result["languages"] == {}


async def test_rate_limited_reraises() -> None:
    fake_client = AsyncMock()
    fake_client.fetch_user_signals = AsyncMock(
        side_effect=GitHubRateLimited(retry_after=42)
    )

    with patch(f"{_ACTIVITY_MODULE}.get_github_client", return_value=fake_client):
        with pytest.raises(GitHubRateLimited) as exc_info:
            await fetch_github_signals("rate-limited-user")

    assert exc_info.value.retry_after == 42


async def test_success_returns_populated_dict() -> None:
    fake_client = AsyncMock()
    fake_client.fetch_user_signals = AsyncMock(
        return_value=ClientGitHubSignals(
            repo_count=12,
            top_language="Python",
            commits_12m=87,
            stars_total=345,
            languages={"Python": 8, "TypeScript": 4},
        )
    )

    with patch(f"{_ACTIVITY_MODULE}.get_github_client", return_value=fake_client):
        result = await fetch_github_signals("testuser")

    assert result["repo_count"] == 12
    assert result["top_language"] == "Python"
    assert result["commits_12m"] == 87
    assert result["stars_total"] == 345
    assert result["languages"] == {"Python": 8, "TypeScript": 4}
    fake_client.fetch_user_signals.assert_awaited_once_with("testuser")
