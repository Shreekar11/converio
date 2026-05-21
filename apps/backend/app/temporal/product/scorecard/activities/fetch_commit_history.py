"""scorecard.fetch_commit_history — aggregate commit cadence over a trailing window.

Used by the planner for `consistency_over_time` and `language_depth` low-conf
dimensions. The output is a *summary*, never raw commit list — keeps activity
payload small and avoids leaking commit messages into Langfuse traces.

Sources of truth (in priority order):
1. `/users/{u}/events/public` — push events tell us which repos got commits
   and when. GitHub caps this feed at ~300 events / 90 days, so this gives us
   precise cadence for the most recent quarter only.
2. `/users/{u}/repos?sort=pushed` — the per-repo `pushed_at` timestamp gives
   us coarser-grained activity over a longer trailing window (24mo). We use
   this to compute `active_months` and the `consistency_score`.

Replay determinism notes:

* No `datetime.now()` calls anywhere — the trailing window is computed off
  a `now_iso` field that defaults to the *activity-info* current time
  (which is captured in workflow history and replayed deterministically).
  The activity is the right boundary to read wall-clock from — the
  *workflow* must never do so.
* The `since_days` knob is accepted from the planner so the LLM can ask
  for a shorter or longer trailing window when context calls for it.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

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

# Window over which `active_months` and `consistency_score` are computed.
# 24 months gives the LLM enough signal to distinguish steady contributors
# from one-quarter bursts.
_TRAILING_WINDOW_MONTHS = 24


class FetchCommitHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(..., min_length=1)
    since_days: int = Field(
        default=365,
        ge=1,
        le=365 * 5,
        description="Trailing window for `total_commits_last_year` (default 1y).",
    )
    # Optional reference time — caller (workflow) can pass an ISO timestamp
    # to fix the "now" anchor for deterministic re-runs in tests. When None,
    # we read it from the Temporal activity context (which is captured to
    # history on first attempt and replayed on subsequent attempts).
    now_iso: str | None = Field(
        default=None,
        description="Reference 'now' (ISO 8601). None => use activity-info current time.",
    )


class CommitHistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_commits_last_year: int = 0
    active_months: int = Field(
        default=0,
        description="Months with >=1 commit in the trailing 24-month window.",
    )
    repos_contributed_to: list[str] = Field(default_factory=list)
    primary_languages: list[str] = Field(default_factory=list)
    consistency_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="active_months / 24 — fraction of trailing 24mo with activity.",
    )


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # GitHub returns "Z" suffix; Python's fromisoformat accepts it on 3.11+.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_anchor(now_iso: str | None) -> datetime:
    """Resolve the reference 'now'. Reads from activity context if not
    explicitly passed — activity context is captured to workflow history,
    so this stays replay-safe."""
    parsed = _parse_iso(now_iso)
    if parsed is not None:
        return parsed
    # `activity.info().started_time` is captured in history on first attempt
    # and re-emitted on replay — safe to read from activity code (it's NOT
    # safe to read from workflow code).
    try:
        info = activity.info()
        if info.started_time:
            return info.started_time
    except Exception:
        pass
    # Last-resort fallback — only reachable in unit tests that call the
    # activity function directly without an activity context.
    return datetime.now(timezone.utc)


@ActivityRegistry.register("scorecard", "fetch_commit_history")
@activity.defn(name="scorecard.fetch_commit_history")
async def fetch_commit_history(payload: dict) -> dict:
    """Aggregate commit-cadence signals over a trailing window."""
    input_model = FetchCommitHistoryInput.model_validate(payload)
    username = input_model.github_username
    now = _now_anchor(input_model.now_iso)
    since_year = now - timedelta(days=input_model.since_days)
    since_24mo = now - timedelta(days=_TRAILING_WINDOW_MONTHS * 30)

    client = get_github_client()

    # --- 1. Push events (precise but capped at ~300 events / ~90 days) ---
    try:
        events = await client.list_user_events(username, per_page=100, max_pages=3)
    except GitHubNotFound:
        LOGGER.info(
            "fetch_commit_history: GitHub user not found — empty summary",
            extra={"username": username},
        )
        return CommitHistorySummary().model_dump(mode="json")
    except GitHubRateLimitError:
        raise

    total_commits_last_year = 0
    active_months: set[str] = set()
    repos_from_events: set[str] = set()

    for event in events:
        if event.get("type") != "PushEvent":
            continue
        created_at = _parse_iso(event.get("created_at"))
        if not created_at:
            continue
        payload_obj = event.get("payload") or {}
        commit_count = len(payload_obj.get("commits") or []) or 1

        if created_at >= since_year:
            total_commits_last_year += commit_count

        if created_at >= since_24mo:
            # `YYYY-MM` is a stable, sortable month key.
            active_months.add(f"{created_at.year:04d}-{created_at.month:02d}")
            repo_name = (event.get("repo") or {}).get("name")
            if isinstance(repo_name, str):
                repos_from_events.add(repo_name)

    # --- 2. Repo `pushed_at` for the longer 24-month tail ---
    try:
        repos = await client.list_user_repos(
            username, per_page=100, max_pages=1, sort="pushed", repo_type="owner"
        )
    except GitHubNotFound:
        repos = []
    except GitHubRateLimitError:
        raise

    repos_recent: list[dict] = []
    for repo in repos:
        pushed = _parse_iso(repo.get("pushed_at"))
        if not pushed:
            continue
        if pushed < since_24mo:
            continue
        repos_recent.append(repo)
        active_months.add(f"{pushed.year:04d}-{pushed.month:02d}")
        full_name = repo.get("full_name")
        if isinstance(full_name, str):
            repos_from_events.add(full_name)

    # Primary languages by repo count (frequency, not byte-weighted — bytes
    # belong to `fetch_repo_languages`). Sorted for replay stability.
    lang_counts: Counter[str] = Counter()
    for repo in repos_recent:
        lang = repo.get("language")
        if isinstance(lang, str) and lang:
            lang_counts[lang] += 1

    primary_languages = [lang for lang, _ in lang_counts.most_common(5)]
    repos_contributed_to = sorted(repos_from_events)
    active_months_count = len(active_months)
    consistency_score = round(
        min(active_months_count / float(_TRAILING_WINDOW_MONTHS), 1.0), 4
    )

    summary = CommitHistorySummary(
        total_commits_last_year=total_commits_last_year,
        active_months=active_months_count,
        repos_contributed_to=repos_contributed_to,
        primary_languages=primary_languages,
        consistency_score=consistency_score,
    )
    LOGGER.info(
        "fetch_commit_history",
        extra={
            "username": username,
            "commits_1y": summary.total_commits_last_year,
            "active_months": summary.active_months,
            "consistency": summary.consistency_score,
            "since_days": input_model.since_days,
        },
    )
    return summary.model_dump(mode="json")
