"""Scorecard Generator Agent (Agent 4) — activities package.

Importing this package side-effect-registers every `scorecard.*` activity
with both the process-wide `ActivityRegistry` and Temporal's `@activity.defn`
registry. Worker startup imports `app.temporal.product.scorecard` which in
turn imports this package, so a single import path is enough to wire all
activities into the worker.

Phase layout:

* Phase 2 — GitHub evidence-fetcher activities (this file imports all five).
* Phase 3 — deterministic (non-LLM) activities:
    * `scorecard.build_scoring_prompt` — pure prompt assembly
    * `scorecard.check_confidence_gate` — confidence threshold filter
    * `scorecard.compute_overall_match_score` — Decimal-exact weighted avg
    * `scorecard.resolve_citations` — 3-tier citation resolver
    * `scorecard.persist_scorecard` — idempotent upsert into `scorecards`
* Phase 4 — LLM activities (`select_evidence_source`, `rescore_dimension`,
  initial scoring activity, `mark_scorecard_done`).

Phase 2 fetchers and Phase 3 deterministic activities ship in parallel.
Where a Phase 3 activity file has not yet been added, the import is wrapped
in a guarded try/except so that the Phase 2 import path (and the
`fetch_*` smoke-test command) remains usable on a half-built tree.
"""
from __future__ import annotations

# --- Phase 2 fetcher activities --------------------------------------------
# All five are required for the worker to handle scorecard self-correction
# decisions, so import failures here ARE real bugs — surface them.
from app.temporal.product.scorecard.activities.fetch_commit_history import (  # noqa: F401
    CommitHistorySummary,
    FetchCommitHistoryInput,
    fetch_commit_history,
)
from app.temporal.product.scorecard.activities.fetch_pr_review_history import (  # noqa: F401
    FetchPrReviewHistoryInput,
    FetchPrReviewHistoryOutput,
    PrReview,
    fetch_pr_review_history,
)
from app.temporal.product.scorecard.activities.fetch_repo_languages import (  # noqa: F401
    FetchRepoLanguagesInput,
    LanguageBreakdown,
    fetch_repo_languages,
)
from app.temporal.product.scorecard.activities.fetch_repo_readmes import (  # noqa: F401
    FetchRepoReadmesInput,
    FetchRepoReadmesOutput,
    RepoReadme,
    fetch_repo_readmes,
)
from app.temporal.product.scorecard.activities.fetch_user_orgs_and_stars import (  # noqa: F401
    FetchUserOrgsAndStarsInput,
    OrgsAndStars,
    fetch_user_orgs_and_stars,
)

# --- Phase 3 deterministic activities --------------------------------------
# Imports are guarded so a missing Phase 3 file does not break Phase 2
# acceptance checks. Once Phase 3 lands fully, the guards become noise but
# stay harmless. Each activity file uses the same `@activity.defn` +
# `ActivityRegistry.register` pattern as Phase 2.
try:  # pragma: no cover — import-ordering safety net
    from app.temporal.product.scorecard.activities.build_scoring_prompt import (  # noqa: F401
        build_scoring_prompt,
    )
except ImportError:  # pragma: no cover
    pass

try:  # pragma: no cover
    from app.temporal.product.scorecard.activities.check_confidence_gate import (  # noqa: F401
        check_confidence_gate,
    )
except ImportError:  # pragma: no cover
    pass

try:  # pragma: no cover
    from app.temporal.product.scorecard.activities.compute_overall_match_score import (  # noqa: F401
        compute_overall_match_score,
    )
except ImportError:  # pragma: no cover
    pass

try:  # pragma: no cover
    from app.temporal.product.scorecard.activities.persist_scorecard import (  # noqa: F401
        persist_scorecard,
    )
except ImportError:  # pragma: no cover
    pass

try:  # pragma: no cover
    from app.temporal.product.scorecard.activities.resolve_citations import (  # noqa: F401
        resolve_citations,
    )
except ImportError:  # pragma: no cover
    pass

# --- Phase 4 LLM activities ------------------------------------------------
# All four Phase 4 activity files are added in this phase; their imports
# MUST succeed for the worker to register them with Temporal. Unlike the
# Phase 3 try/except guards above (which exist because Phase 3 ships in
# parallel and may land before/after Phase 4), Phase 4 imports here are
# unguarded — any ImportError is a real bug to surface.
from app.temporal.product.scorecard.activities.mark_scorecard_done import (  # noqa: F401
    MarkScorecardDoneInput,
    MarkScorecardDoneOutput,
    mark_scorecard_done,
)
from app.temporal.product.scorecard.activities.rescore_dimension import (  # noqa: F401
    RescoreDimensionInput,
    RescoreDimensionOutput,
    rescore_dimension,
)
from app.temporal.product.scorecard.activities.score_candidate_dimensions import (  # noqa: F401
    ScoreCandidateDimensionsInput,
    ScoreCandidateDimensionsOutput,
    score_candidate_dimensions,
)
from app.temporal.product.scorecard.activities.select_evidence_source import (  # noqa: F401
    SelectEvidenceSourceInput,
    SelectEvidenceSourceOutput,
    select_evidence_source,
)

# --- Phase 6 DB-fetch activities -------------------------------------------
# Loaded by the workflow at the head of the pipeline. Imports are
# unguarded — the workflow strictly requires both to register on the
# worker for ScorecardGeneratorWorkflow to run end-to-end.
from app.temporal.product.scorecard.activities.get_candidate_profile import (  # noqa: F401
    GetCandidateProfileInput,
    GetCandidateProfileOutput,
    get_candidate_profile,
)
from app.temporal.product.scorecard.activities.get_job_rubric import (  # noqa: F401
    GetJobRubricInput,
    GetJobRubricOutput,
    get_job_rubric,
)
