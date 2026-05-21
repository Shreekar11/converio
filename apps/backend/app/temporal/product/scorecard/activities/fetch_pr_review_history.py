"""scorecard.fetch_pr_review_history — surface PR reviews the user has authored.

Used by the planner for `open_source_signals` low-conf dimension. PR reviews
on others' repos are a high-quality signal of OSS engagement that bare
commit counts can't capture.

Data source: `/users/{u}/events/public` filtered to `PullRequestReviewEvent`.
That feed is capped at ~300 events / ~90 days. We therefore return whatever
we get — the LLM is prompted to treat "no signal" as inconclusive rather
than negative (the user may simply have older activity).
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

# Bound the number of reviews returned — the planner only needs ~20 examples
# to assess engagement quality, and Temporal activity payload size is finite.
_MAX_REVIEWS = 50

_VALID_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}


class FetchPrReviewHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(..., min_length=1)


class PrReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_name: str
    pr_title: str
    review_type: str  # "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED"
    created_at: str  # ISO 8601


class FetchPrReviewHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[PrReview] = Field(default_factory=list)
    total_count: int = 0


@ActivityRegistry.register("scorecard", "fetch_pr_review_history")
@activity.defn(name="scorecard.fetch_pr_review_history")
async def fetch_pr_review_history(payload: dict) -> dict:
    """Return recent PR reviews authored by the user."""
    input_model = FetchPrReviewHistoryInput.model_validate(payload)
    username = input_model.github_username

    client = get_github_client()
    try:
        events = await client.list_user_events(username, per_page=100, max_pages=3)
    except GitHubNotFound:
        LOGGER.info(
            "fetch_pr_review_history: GitHub user not found — empty result",
            extra={"username": username},
        )
        return FetchPrReviewHistoryOutput().model_dump(mode="json")
    except GitHubRateLimitError:
        raise

    reviews: list[PrReview] = []
    for event in events:
        if event.get("type") != "PullRequestReviewEvent":
            continue
        event_payload = event.get("payload") or {}
        review = event_payload.get("review") or {}
        pr = event_payload.get("pull_request") or {}

        state_raw = review.get("state") or ""
        review_type = state_raw.upper() if isinstance(state_raw, str) else ""
        if review_type not in _VALID_REVIEW_STATES:
            # Fold unknown states (e.g. DISMISSED) into COMMENTED rather
            # than dropping the row — the LLM still benefits from the
            # repo/title context.
            review_type = "COMMENTED"

        repo_name = (event.get("repo") or {}).get("name") or ""
        pr_title = pr.get("title") or ""
        created_at = review.get("submitted_at") or event.get("created_at") or ""

        if not repo_name or not created_at:
            continue

        reviews.append(
            PrReview(
                repo_name=str(repo_name),
                pr_title=str(pr_title),
                review_type=review_type,
                created_at=str(created_at),
            )
        )
        if len(reviews) >= _MAX_REVIEWS:
            break

    # Sort descending by created_at for replay-stable, UI-friendly order.
    reviews.sort(key=lambda r: r.created_at, reverse=True)

    result = FetchPrReviewHistoryOutput(reviews=reviews, total_count=len(reviews))
    LOGGER.info(
        "fetch_pr_review_history",
        extra={"username": username, "returned": result.total_count},
    )
    return result.model_dump(mode="json")
