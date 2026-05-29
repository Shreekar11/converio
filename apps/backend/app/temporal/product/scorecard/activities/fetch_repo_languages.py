"""scorecard.fetch_repo_languages — byte-weighted language breakdown.

Used by the planner for `language_depth` low-conf dimension. We aggregate
`/repos/{o}/{r}/languages` (bytes-per-language) across the user's owned
repos to surface a *byte-weighted* breakdown — frequency-by-repo-count
(which is what `fetch_commit_history` exposes) overweights tiny utility
repos. Byte weight better reflects which languages the candidate actually
ships.

API budget: this is the most expensive of the five fetchers — one
`/languages` request per repo. We bound it at `_MAX_REPOS_TO_AGGREGATE`
to keep the activity within the GitHub auth'd budget (5000 req/hr).
"""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.core.github_client import (
    GitHubNotFound,
    GitHubRateLimitError,
    get_github_client,
)
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Hard cap on per-repo `/languages` calls. 20 covers any realistic
# top-of-portfolio analysis; beyond that, languages don't shift materially.
_MAX_REPOS_TO_AGGREGATE = 20


class FetchRepoLanguagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(..., min_length=1)


class LanguageBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: dict[str, int] = Field(
        default_factory=dict,
        description="Bytes per language summed across the user's top repos.",
    )
    primary_language: str | None = None
    language_count: int = 0


@ActivityRegistry.register("scorecard", "fetch_repo_languages")
@activity.defn(name="scorecard.fetch_repo_languages")
async def fetch_repo_languages(payload: dict) -> dict:
    """Aggregate byte-weighted language breakdown across the user's repos."""
    input_model = FetchRepoLanguagesInput.model_validate(payload)
    username = input_model.github_username

    client = get_github_client()
    try:
        repos = await client.list_user_repos(
            username, per_page=100, max_pages=1, sort="updated", repo_type="owner"
        )
    except GitHubNotFound:
        LOGGER.info(
            "fetch_repo_languages: GitHub user not found — empty result",
            extra={"username": username},
        )
        return LanguageBreakdown().model_dump(mode="json")
    except GitHubRateLimitError:
        raise

    # Skip forks — they're not the user's own language choice; including
    # them inflates the language they happened to fork in. Plan §16 calls
    # this out implicitly via "primary language" semantics.
    own_repos = [r for r in repos if not r.get("fork")][:_MAX_REPOS_TO_AGGREGATE]

    aggregate: Counter[str] = Counter()
    for repo in own_repos:
        owner = (repo.get("owner") or {}).get("login") or username
        name = repo.get("name")
        if not name:
            continue
        try:
            per_repo = await client.get_repo_languages(owner, name)
        except GitHubRateLimitError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            LOGGER.warning(
                "fetch_repo_languages: per-repo aggregation failed; skipping repo",
                extra={"username": username, "repo": name, "error": str(exc)},
            )
            continue
        for lang, byte_count in per_repo.items():
            aggregate[lang] += int(byte_count)

    if not aggregate:
        result = LanguageBreakdown()
    else:
        # Sort for deterministic dict ordering across replays.
        sorted_items = sorted(
            aggregate.items(), key=lambda kv: (-kv[1], kv[0])
        )
        languages_dict = {lang: bytes_count for lang, bytes_count in sorted_items}
        result = LanguageBreakdown(
            languages=languages_dict,
            primary_language=sorted_items[0][0],
            language_count=len(languages_dict),
        )

    LOGGER.info(
        "fetch_repo_languages",
        extra={
            "username": username,
            "repos_aggregated": len(own_repos),
            "languages": result.language_count,
            "primary": result.primary_language,
        },
    )
    return result.model_dump(mode="json")
