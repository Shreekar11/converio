"""Tests for app.core.github_client.GitHubClient using respx-mocked httpx."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.core.github_client import GitHubClient, GitHubNotFound, GitHubRateLimited


@pytest.fixture
def github_client() -> GitHubClient:
    return GitHubClient(token="test-token")


@respx.mock
async def test_fetch_user_signals_success(github_client: GitHubClient) -> None:
    respx.get("https://api.github.com/users/testuser").mock(
        return_value=httpx.Response(
            200,
            json={"login": "testuser", "public_repos": 15},
        )
    )
    respx.get("https://api.github.com/users/testuser/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"stargazers_count": 10, "language": "Python"},
                {"stargazers_count": 5, "language": "TypeScript"},
                {"stargazers_count": 2, "language": "Python"},
            ],
        )
    )
    respx.get("https://api.github.com/users/testuser/events/public").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"type": "PushEvent"},
                {"type": "PushEvent"},
                {"type": "WatchEvent"},
            ],
        )
    )

    signals = await github_client.fetch_user_signals("testuser")

    assert signals.repo_count == 15
    assert signals.top_language == "Python"
    assert signals.commits_12m == 2
    assert signals.stars_total == 17
    assert signals.languages == {"Python": 2, "TypeScript": 1}


@respx.mock
async def test_fetch_user_not_found(github_client: GitHubClient) -> None:
    respx.get("https://api.github.com/users/doesnotexist").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(GitHubNotFound):
        await github_client.fetch_user_signals("doesnotexist")


@respx.mock
async def test_fetch_rate_limited(github_client: GitHubClient) -> None:
    respx.get("https://api.github.com/users/testuser").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "5"},
            json={"message": "rate limited"},
        )
    )

    with pytest.raises(GitHubRateLimited) as exc_info:
        await github_client.fetch_user_signals("testuser")

    assert exc_info.value.retry_after == 5
