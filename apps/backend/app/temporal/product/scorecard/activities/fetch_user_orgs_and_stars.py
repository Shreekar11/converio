"""scorecard.fetch_user_orgs_and_stars — org membership + starred-repo signals.

Used by the planner for `open_source_signals` low-conf dimension. Org
membership and the user's starred set together paint a picture of who
the candidate engages with in OSS — companies they've worked at publicly,
projects they care about, ecosystem-level interests.

Notable repos = starred repos with >1000 stars. This filters out personal
projects and surfaces well-known OSS the candidate has explicitly flagged.
"""
from __future__ import annotations

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

# Bound on `notable_repos_starred` returned to the LLM. The signal is
# qualitative — 30 well-known repos is plenty of context.
_MAX_NOTABLE_RETURNED = 30
_NOTABLE_STAR_THRESHOLD = 1000


class FetchUserOrgsAndStarsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(..., min_length=1)


class OrgsAndStars(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organizations: list[str] = Field(default_factory=list)
    total_starred_repos: int = 0
    notable_repos_starred: list[str] = Field(
        default_factory=list,
        description="Repo full_names (owner/repo) with >1000 stars; capped at 30.",
    )


@ActivityRegistry.register("scorecard", "fetch_user_orgs_and_stars")
@activity.defn(name="scorecard.fetch_user_orgs_and_stars")
async def fetch_user_orgs_and_stars(payload: dict) -> dict:
    """Pull a user's public orgs and their starred-repo profile."""
    input_model = FetchUserOrgsAndStarsInput.model_validate(payload)
    username = input_model.github_username

    client = get_github_client()

    # --- 1. Orgs --------------------------------------------------------
    try:
        orgs_raw = await client.list_user_orgs(username)
    except GitHubNotFound:
        LOGGER.info(
            "fetch_user_orgs_and_stars: GitHub user not found — empty result",
            extra={"username": username},
        )
        return OrgsAndStars().model_dump(mode="json")
    except GitHubRateLimitError:
        raise

    organizations = sorted({
        str(o.get("login"))
        for o in orgs_raw
        if isinstance(o, dict) and o.get("login")
    })

    # --- 2. Stars -------------------------------------------------------
    try:
        starred = await client.list_user_starred(username, per_page=100, max_pages=2)
    except GitHubNotFound:
        starred = []
    except GitHubRateLimitError:
        raise

    total_starred = len(starred)
    notable: list[tuple[int, str]] = []
    for repo in starred:
        stars = int(repo.get("stargazers_count") or 0)
        if stars < _NOTABLE_STAR_THRESHOLD:
            continue
        full_name = repo.get("full_name")
        if not isinstance(full_name, str):
            continue
        notable.append((stars, full_name))

    # Sort by stars desc, then by name for replay-stable ordering.
    notable.sort(key=lambda t: (-t[0], t[1]))
    notable_names = [name for _, name in notable[:_MAX_NOTABLE_RETURNED]]

    result = OrgsAndStars(
        organizations=organizations,
        total_starred_repos=total_starred,
        notable_repos_starred=notable_names,
    )
    LOGGER.info(
        "fetch_user_orgs_and_stars",
        extra={
            "username": username,
            "orgs": len(result.organizations),
            "total_starred": result.total_starred_repos,
            "notable": len(result.notable_repos_starred),
        },
    )
    return result.model_dump(mode="json")
