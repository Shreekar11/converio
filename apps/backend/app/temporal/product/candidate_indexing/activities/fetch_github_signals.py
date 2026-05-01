from temporalio import activity

from app.core.github_client import GitHubNotFound, GitHubRateLimited, get_github_client
from app.schemas.product.candidate import GitHubSignals
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("candidate_indexing", "fetch_github_signals")
@activity.defn
async def fetch_github_signals(github_username: str | None) -> dict:
    """Fetch GitHub signals for username. Returns empty GitHubSignals if username is None or not found."""
    if not github_username:
        LOGGER.info("No GitHub username — skipping fetch")
        return GitHubSignals().model_dump(mode="json")

    LOGGER.info("Fetching GitHub signals", extra={"username": github_username})

    client = get_github_client()
    try:
        signals = await client.fetch_user_signals(github_username)
        # Convert github_client dataclass to Pydantic GitHubSignals for workflow IO
        pydantic_signals = GitHubSignals(
            repo_count=signals.repo_count,
            top_language=signals.top_language,
            commits_12m=signals.commits_12m,
            stars_total=signals.stars_total,
            languages=signals.languages,
        )
        LOGGER.info(
            "GitHub signals fetched",
            extra={
                "username": github_username,
                "repos": signals.repo_count,
                "top_lang": signals.top_language,
                "commits_12m": signals.commits_12m,
            },
        )
        return pydantic_signals.model_dump(mode="json")

    except GitHubNotFound:
        LOGGER.warning("GitHub user not found — returning empty signals", extra={"username": github_username})
        return GitHubSignals().model_dump(mode="json")

    except GitHubRateLimited as e:
        LOGGER.error(
            "GitHub rate limited — activity will retry",
            extra={"username": github_username, "retry_after": e.retry_after},
        )
        # Re-raise so Temporal retries with exponential backoff
        # RetryPolicy on this activity: max=5, backoff=2.0, max_interval=120s
        raise
