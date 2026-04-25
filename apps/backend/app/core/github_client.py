import os
from dataclasses import dataclass, field

import httpx

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_GITHUB_API = "https://api.github.com"


class GitHubNotFound(Exception):
    """GitHub user or resource does not exist."""


class GitHubRateLimited(Exception):
    """GitHub API rate limit hit."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"GitHub rate limited — retry after {retry_after}s")


@dataclass
class GitHubSignals:
    repo_count: int = 0
    top_language: str | None = None
    commits_12m: int = 0
    stars_total: int = 0
    languages: dict[str, int] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return self.repo_count == 0


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        _token = token or os.getenv("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if _token:
            headers["Authorization"] = f"Bearer {_token}"
        self._client = httpx.AsyncClient(
            base_url=_GITHUB_API,
            headers=headers,
            timeout=30.0,
        )

    async def fetch_user_signals(self, username: str) -> GitHubSignals:
        """Fetch public signals for a GitHub user."""
        # Fetch user profile
        user_resp = await self._get(f"/users/{username}")
        public_repos: int = user_resp.get("public_repos", 0)

        # Fetch repos (sorted by updated, take top 30 for language analysis)
        repos_resp = await self._get(
            f"/users/{username}/repos",
            params={"sort": "updated", "per_page": 30, "type": "owner"},
        )

        stars_total = sum(r.get("stargazers_count", 0) for r in repos_resp)
        lang_counts: dict[str, int] = {}
        for repo in repos_resp:
            lang = repo.get("language")
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

        top_language = max(lang_counts, key=lang_counts.get) if lang_counts else None

        # Estimate commits in last 12 months via events (best-effort, may be capped at 300)
        commits_12m = await self._estimate_commits_12m(username)

        return GitHubSignals(
            repo_count=public_repos,
            top_language=top_language,
            commits_12m=commits_12m,
            stars_total=stars_total,
            languages=lang_counts,
        )

    async def _estimate_commits_12m(self, username: str) -> int:
        try:
            events = await self._get(
                f"/users/{username}/events/public",
                params={"per_page": 100},
            )
            return sum(
                1 for e in events
                if e.get("type") == "PushEvent"
            )
        except Exception:
            return 0

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        resp = await self._client.get(path, params=params)

        if resp.status_code == 404:
            raise GitHubNotFound(f"GitHub resource not found: {path}")

        if resp.status_code == 429 or resp.status_code == 403:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise GitHubRateLimited(retry_after=retry_after)

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < 5:
            LOGGER.warning("GitHub rate limit nearly exhausted", extra={"remaining": remaining})

        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


_client: GitHubClient | None = None


def get_github_client() -> GitHubClient:
    """Process-wide singleton."""
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client


async def close_github_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
