"""scorecard.fetch_repo_readmes — pull READMEs for repos matching a keyword filter.

Used by the self-correction planner when a low-confidence dimension needs
deeper evidence than the resume alone provides (e.g. `distributed_systems_depth`
asks for repos mentioning kafka/redis/grpc).

Design notes:

* The keyword filter is applied to repo *metadata* first (name, description,
  topics, language) so we only spend README API calls on plausible candidates.
  This is critical — fetching a README is a separate API request per repo and
  GitHub unauthed budget is 60 req/hr.
* If metadata filtering yields no candidates, we still fetch a few READMEs
  from the most recently-updated repos and apply the keyword filter to the
  README text itself — this catches repos whose topic/description didn't
  surface the relevant signal but whose README does.
* README text is truncated to 2000 chars per the schema contract — anything
  longer is noise for the rescore prompt and bloats activity payload size
  (Temporal has a default 2MiB limit per activity result).
* `keyword_filter` is required but may be empty; an empty filter returns
  READMEs for the top-updated repos unconditionally (planner's escape hatch
  when it can't pick keywords).
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

# Cap on how many candidate repos we even consider before keyword filtering.
# Tightens the API budget — at 100/hr unauthed, we cannot afford to scan
# arbitrarily large user catalogs.
_MAX_CANDIDATE_REPOS = 30

# Cap on how many READMEs we actually fetch after filtering. Even if a user
# has 100 repos matching the filter, the rescore prompt does not benefit
# from more than ~10 evidence chunks.
_MAX_README_FETCHES = 10

# Hard cap on README text size in characters — see module docstring.
_README_CHAR_CAP = 2000


class FetchRepoReadmesInput(BaseModel):
    """Activity input. `keyword_filter` is case-insensitive and OR-combined."""

    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(..., min_length=1)
    keyword_filter: list[str] = Field(
        default_factory=list,
        description="OR-combined, case-insensitive. Empty list = no filter.",
    )


class RepoReadme(BaseModel):
    """A single repo with its README and metadata. Schema is consumed by the
    rescore LLM prompt — keep field names stable across Phase 2/3/4."""

    model_config = ConfigDict(extra="forbid")

    repo_name: str
    description: str | None
    readme_text: str | None
    languages: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    stars: int = 0


class FetchRepoReadmesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[RepoReadme] = Field(default_factory=list)
    candidate_repo_count: int = Field(
        default=0,
        description="Number of repos considered before README filtering — diagnostic only.",
    )
    matched_repo_count: int = Field(
        default=0,
        description="Number of repos that passed metadata filter — diagnostic only.",
    )


def _matches_keywords(text: str | None, keywords_lower: list[str]) -> bool:
    """Substring match — sufficient for repo metadata which is short. The
    plan explicitly accepts OR-combination across name/desc/topics."""
    if not keywords_lower:
        return True
    if not text:
        return False
    haystack = text.lower()
    return any(kw in haystack for kw in keywords_lower)


def _repo_metadata_match(repo: dict, keywords_lower: list[str]) -> bool:
    """Apply the keyword filter to repo *metadata* — name/description/topics/
    primary language. Cheaper than fetching the README."""
    if not keywords_lower:
        return True
    fields_to_check: list[str | None] = [
        repo.get("name"),
        repo.get("description"),
        repo.get("language"),
    ]
    if any(_matches_keywords(f, keywords_lower) for f in fields_to_check):
        return True
    topics = repo.get("topics") or []
    if isinstance(topics, list):
        combined_topics = " ".join(str(t) for t in topics)
        if _matches_keywords(combined_topics, keywords_lower):
            return True
    return False


@ActivityRegistry.register("scorecard", "fetch_repo_readmes")
@activity.defn(name="scorecard.fetch_repo_readmes")
async def fetch_repo_readmes(payload: dict) -> dict:
    """Fetch READMEs for repos matching the keyword filter."""
    input_model = FetchRepoReadmesInput.model_validate(payload)
    username = input_model.github_username
    keywords_lower = [k.lower() for k in input_model.keyword_filter if k]

    client = get_github_client()

    try:
        repos = await client.list_user_repos(
            username,
            per_page=100,
            max_pages=1,
            sort="updated",
            repo_type="owner",
        )
    except GitHubNotFound:
        LOGGER.info(
            "fetch_repo_readmes: GitHub user not found — returning empty result",
            extra={"username": username},
        )
        return FetchRepoReadmesOutput().model_dump(mode="json")
    except GitHubRateLimitError:
        # Re-raise so Temporal retries with the activity-level retry policy.
        raise

    candidate_repos = repos[:_MAX_CANDIDATE_REPOS]
    matched_via_metadata: list[dict] = [
        r for r in candidate_repos if _repo_metadata_match(r, keywords_lower)
    ]

    # Bound the README fetch budget regardless of how many matched metadata.
    to_fetch = matched_via_metadata[:_MAX_README_FETCHES]

    # If metadata filtering shut us out entirely AND we have keywords to apply,
    # take a second pass: fetch READMEs for the top recently-updated repos and
    # re-test the keyword filter against the README body. This is the README-
    # body branch of the OR contract.
    readme_body_fallback = False
    if not to_fetch and keywords_lower and candidate_repos:
        to_fetch = candidate_repos[:_MAX_README_FETCHES]
        readme_body_fallback = True

    out_repos: list[RepoReadme] = []
    for repo in to_fetch:
        owner_login = (repo.get("owner") or {}).get("login") or username
        repo_name = repo.get("name")
        if not repo_name:
            continue

        try:
            readme_raw = await client.get_readme(owner_login, repo_name)
        except GitHubRateLimitError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            LOGGER.warning(
                "fetch_repo_readmes: failed to fetch README; skipping repo",
                extra={"username": username, "repo": repo_name, "error": str(exc)},
            )
            readme_raw = None

        readme_text = readme_raw[:_README_CHAR_CAP] if readme_raw else None

        # README-body keyword check (only when in fallback path).
        if (
            readme_body_fallback
            and keywords_lower
            and not _matches_keywords(readme_text, keywords_lower)
        ):
            continue

        # Pull a small ordered set of language names. Single-language repos
        # are the common case — we deliberately do NOT make an extra
        # /languages API call here per-repo (that would double the API spend);
        # the `fetch_repo_languages` activity is the right place for that.
        primary_lang = repo.get("language")
        languages = [primary_lang] if isinstance(primary_lang, str) else []

        topics_raw = repo.get("topics") or []
        topics = (
            sorted({str(t) for t in topics_raw if t})
            if isinstance(topics_raw, list)
            else []
        )

        out_repos.append(
            RepoReadme(
                repo_name=str(repo_name),
                description=repo.get("description"),
                readme_text=readme_text,
                languages=languages,
                topics=topics,
                stars=int(repo.get("stargazers_count") or 0),
            )
        )

    # Sort by stars desc, then by name for replay-stable ordering.
    out_repos.sort(key=lambda r: (-r.stars, r.repo_name))

    result = FetchRepoReadmesOutput(
        repos=out_repos,
        candidate_repo_count=len(candidate_repos),
        matched_repo_count=len(matched_via_metadata),
    )
    LOGGER.info(
        "fetch_repo_readmes",
        extra={
            "username": username,
            "keyword_filter": input_model.keyword_filter,
            "candidates": result.candidate_repo_count,
            "matched_meta": result.matched_repo_count,
            "returned": len(result.repos),
            "fallback": readme_body_fallback,
        },
    )
    return result.model_dump(mode="json")
