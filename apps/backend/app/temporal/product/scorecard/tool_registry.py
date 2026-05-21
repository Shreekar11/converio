"""Scorecard Generator Agent — tool catalog.

Defines `SCORECARD_TOOLS`: the typed catalog of tools the Scorecard Agent's
LLM decision loop may invoke. Each `ToolSpec` maps 1:1 to a Temporal activity
registered under the `scorecard.*` namespace and reuses the shared core
abstractions in `app.temporal.core.tool_registry`.

The module also registers the v2 reserved tool names (`fetch_blog_posts`,
`fetch_linkedin_recommendations`, `fetch_conference_talks`,
`fetch_huggingface_contributions`, `fetch_arxiv_papers`) as
`NotImplementedYet`-backed stubs. Reserving these names today keeps the LLM
prompt contract stable across v1 → v2: prompts that reference these tools
will neither crash nor accidentally route to an unrelated activity. When v2
ships, the activity_name targets are flipped from `scorecard.v2.<name>` to
their real implementations and the stubs are removed.

Import side effects
-------------------
Importing this module registers all 13 specs (8 MVP + 5 v2 stubs) into the
process-wide `_TOOL_REGISTRY` from `core.tool_registry`. Worker bootstrap
must import this module exactly once before starting the Scorecard workflow.
The MVP-only catalog is exported as `SCORECARD_TOOLS` so the workflow can
render only the live tools into the LLM prompt.

Usage
-----
    from app.temporal.product.scorecard.tool_registry import SCORECARD_TOOLS
    from app.temporal.core.tool_registry import render_tools_for_llm

    tool_catalog_md = render_tools_for_llm(SCORECARD_TOOLS)
"""

from __future__ import annotations

from app.temporal.core.tool_registry import (
    CostClass,
    NotImplementedYet,
    ToolSpec,
    register_tool,
)
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# 8.1 MVP tools (v1 — GitHub-only)
# ---------------------------------------------------------------------------
# All five fetchers are FREE (read-only GitHub REST calls) and idempotent —
# repeated calls within the same workflow execution return the same result,
# so the LLM is expected to memoize-by-not-calling rather than the workflow
# enforcing it. select_evidence_source is a small Flash-tier structured-output
# call; rescore_dimension is a larger Pro-tier structured-output call and is
# NOT idempotent (the LLM may return slightly different rationales on retry
# because of temperature > 0). mark_scorecard_done is the loop-terminating
# control tool.


_FETCH_REPO_READMES = ToolSpec(
    name="fetch_repo_readmes",
    activity_name="scorecard.fetch_repo_readmes",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Read-only GitHub fetcher. Pulls READMEs (decoded text), descriptions, "
        "language, star count, and last-pushed timestamp for the user's "
        "non-fork repos. Optional `keyword_filter` performs OR-combined, "
        "case-insensitive substring matching against repo name / description "
        "/ README body — used to focus on infra-keyword repos (kafka, redis, "
        "grpc, kubernetes, ...) when scoring `distributed_systems_depth`."
    ),
    when_to_use_md=(
        "Use for `distributed_systems_depth` (filter by infra keywords) and "
        "`system_design_thinking` (filter by 'architecture', 'design', "
        "'rfc'). Call after `select_evidence_source` has picked this tool. "
        "If the keyword-filtered call returns an empty list, fall back to "
        "ONE retry with `keyword_filter=[]` to get the user's top repos "
        "before giving up on the dimension."
    ),
    when_not_to_use_md=(
        "Do not call without a `github_username` — there is no fallback. "
        "Do not call twice with identical args within the same run; the "
        "result is deterministic for a given GitHub snapshot. Do not call "
        "for dimensions like `communication_clarity` where README content "
        "is not relevant — pick the right tool via `select_evidence_source` "
        "first."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "github_username": {
                "type": "string",
                "minLength": 1,
                "description": "GitHub login (the bit after github.com/).",
            },
            "keyword_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "OR-combined, case-insensitive substring filters applied "
                    "across repo name, description, and README. Empty list "
                    "= no filter (returns top repos by stars)."
                ),
            },
        },
        "required": ["github_username"],
    },
    return_description=(
        "`{repos: [{repo_name, description, readme_text, primary_language, "
        "stargazers_count, pushed_at}], total_count: int}`. `readme_text` "
        "is truncated to ~8KB per repo to keep prompts bounded."
    ),
)


_FETCH_COMMIT_HISTORY = ToolSpec(
    name="fetch_commit_history",
    activity_name="scorecard.fetch_commit_history",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Read-only GitHub fetcher. Aggregates the user's public commit "
        "history into a summary: total commits in the trailing window, "
        "per-month bucket counts, and the dominant language by commit "
        "volume. Used as a recency / consistency signal."
    ),
    when_to_use_md=(
        "Use for `consistency_over_time` (24-month window typically) and "
        "as a recency complement to `fetch_repo_languages` for "
        "`language_depth`. A reasonable default is `since_days=365`."
    ),
    when_not_to_use_md=(
        "Do not call with `since_days > 1825` (5y) — older history adds "
        "noise without signal. Do not call for dimensions that are about "
        "repo content (`distributed_systems_depth`); use "
        "`fetch_repo_readmes` instead."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "github_username": {
                "type": "string",
                "minLength": 1,
                "description": "GitHub login.",
            },
            "since_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1825,
                "description": "Trailing window in days for total commits.",
            },
        },
        "required": ["github_username"],
    },
    return_description=(
        "`{total_commits_last_year, commits_by_month: {YYYY-MM: count}, "
        "dominant_language, active_months_in_window}`."
    ),
)


_FETCH_PR_REVIEW_HISTORY = ToolSpec(
    name="fetch_pr_review_history",
    activity_name="scorecard.fetch_pr_review_history",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Read-only GitHub fetcher. Returns PRs the user has reviewed in "
        "repos they do NOT own, with review type (APPROVED / "
        "CHANGES_REQUESTED / COMMENTED) and timestamp. Strong signal for "
        "`open_source_signals` because cross-repo review activity implies "
        "the user is trusted by other maintainers."
    ),
    when_to_use_md=(
        "Use for `open_source_signals`. Useful when "
        "`fetch_user_orgs_and_stars` shows the user has no org "
        "memberships but you suspect external contribution activity."
    ),
    when_not_to_use_md=(
        "Do not call for backend-depth dimensions — review activity does "
        "not tell you what the user can build, only what they have "
        "commented on. Do not call without a `github_username`."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "github_username": {
                "type": "string",
                "minLength": 1,
                "description": "GitHub login.",
            }
        },
        "required": ["github_username"],
    },
    return_description=(
        "`{reviews: [{repo_name, pr_title, review_type, created_at}], "
        "total_count}`. Reviews on the user's own repos are excluded."
    ),
)


_FETCH_REPO_LANGUAGES = ToolSpec(
    name="fetch_repo_languages",
    activity_name="scorecard.fetch_repo_languages",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Read-only GitHub fetcher. Returns a byte-weighted language "
        "breakdown across the user's top non-fork repos, plus the primary "
        "language overall. Bytes-of-code is a coarser but more honest "
        "depth signal than 'languages listed on profile' — it accounts "
        "for the user actually committing code in that language."
    ),
    when_to_use_md=(
        "Use for `language_depth`. Pair with `fetch_commit_history` to "
        "weight by recency (e.g. 'TypeScript was 60% of bytes but only "
        "10% of commits in the last year' is a meaningful signal)."
    ),
    when_not_to_use_md=(
        "Do not call for dimensions unrelated to language proficiency. "
        "Do not call repeatedly — the breakdown is deterministic for a "
        "given snapshot."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "github_username": {
                "type": "string",
                "minLength": 1,
                "description": "GitHub login.",
            }
        },
        "required": ["github_username"],
    },
    return_description=(
        "`{languages: {<lang>: <bytes>}, primary_language, language_count}`."
    ),
)


_FETCH_USER_ORGS_AND_STARS = ToolSpec(
    name="fetch_user_orgs_and_stars",
    activity_name="scorecard.fetch_user_orgs_and_stars",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Read-only GitHub fetcher. Pulls the user's public organization "
        "memberships and the set of notable (>1000 stars) repos they have "
        "starred. Org membership in a well-known engineering org is a "
        "strong trust signal; starred-repo profile reveals technical "
        "interests."
    ),
    when_to_use_md=(
        "Use for `open_source_signals` (paired with "
        "`fetch_pr_review_history`). The orgs list answers 'is this user "
        "actually embedded in an OSS community?'; the starred repos hint "
        "at what they care about technically."
    ),
    when_not_to_use_md=(
        "Do not call for dimensions about code production (write skill, "
        "depth) — stars are a consumption signal, not a production one. "
        "Do not call without a `github_username`."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "github_username": {
                "type": "string",
                "minLength": 1,
                "description": "GitHub login.",
            }
        },
        "required": ["github_username"],
    },
    return_description=(
        "`{organizations: [str], total_starred_repos: int, "
        "notable_repos_starred: [owner/repo]}`. Notable list is capped at "
        "30 entries."
    ),
)


_SELECT_EVIDENCE_SOURCE = ToolSpec(
    name="select_evidence_source",
    activity_name="scorecard.select_evidence_source",
    cost_class=CostClass.MICRO,
    idempotent=True,
    description_md=(
        "Small structured-output LLM call (Gemini Flash class). Given one "
        "low-confidence rubric dimension and the candidate profile "
        "summary, picks the best fetcher from the allowlisted set, or "
        "returns `evidence_limited` if no MVP tool applies. The "
        "workflow narrows `available_tools` as fetchers are consumed so "
        "the planner does not loop on the same dim."
    ),
    when_to_use_md=(
        "Call once per low-confidence dimension, before invoking any "
        "fetcher for that dim. Pass the workflow's current "
        "`available_tools` allowlist verbatim — do not invent tool names."
    ),
    when_not_to_use_md=(
        "Do not call for dims that are already confident (>= 0.65). Do "
        "not call twice for the same dim within a run unless the first "
        "fetch returned empty AND there is a second viable tool in the "
        "allowlist. Do not call with `available_tools=[]` — there is "
        "nothing to choose from."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "low_conf_dim": {
                "type": "object",
                "description": (
                    "Serialized `LowConfDim`: at minimum "
                    "`{name, score, confidence, rationale, weight}`."
                ),
            },
            "candidate_profile_summary": {
                "type": "string",
                "maxLength": 4000,
                "description": (
                    "Compressed candidate profile (resume highlights, "
                    "indexed GitHub signals) for the planner to reason over."
                ),
            },
            "available_tools": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Workflow-controlled allowlist of fetcher tool names "
                    "the planner may pick from on this iteration."
                ),
            },
            "github_username": {
                "type": ["string", "null"],
                "description": (
                    "GitHub login if known; the planner uses presence/"
                    "absence to bias toward or away from GitHub fetchers."
                ),
            },
        },
        "required": [
            "low_conf_dim",
            "candidate_profile_summary",
            "available_tools",
        ],
    },
    return_description=(
        "`{tool_name: str, args: dict, rationale: str}`. `tool_name` is "
        "either a member of `available_tools` or the literal "
        "'evidence_limited' (signaling the dim has no viable v1 "
        "fetcher and should retain its current confidence)."
    ),
)


_RESCORE_DIMENSION = ToolSpec(
    name="rescore_dimension",
    activity_name="scorecard.rescore_dimension",
    cost_class=CostClass.SMALL,
    idempotent=False,
    description_md=(
        "Structured-output LLM call (Gemini Pro class). Given the "
        "original score / confidence / rationale for one dimension AND "
        "the evidence dict returned by a fetcher, emits an updated "
        "`ScorecardDimension` (new score, new confidence, new rationale, "
        "and a citation pointing back into the evidence)."
    ),
    when_to_use_md=(
        "Call immediately after a successful fetcher call for the same "
        "low-confidence dim. Pass the fetcher's full return dict as "
        "`evidence` — the rescore prompt is designed to read fetcher "
        "schemas. Use the original score/confidence/rationale verbatim "
        "from the initial scorecard."
    ),
    when_not_to_use_md=(
        "Do not call without first running a fetcher (or "
        "`select_evidence_source` returning a non-`evidence_limited` "
        "choice). Do not call on already-confident dimensions. Do not "
        "call twice for the same dim with identical evidence — re-runs "
        "will not improve the score and will burn budget."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "dimension_name": {
                "type": "string",
                "minLength": 1,
                "description": "Rubric dimension key (e.g. 'distributed_systems_depth').",
            },
            "original_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Score from the initial structured-output scoring call.",
            },
            "original_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence from the initial scoring call (< 0.65 = low).",
            },
            "original_rationale": {
                "type": "string",
                "description": "Initial scoring rationale (one sentence).",
            },
            "original_weight": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Rubric weight, echoed back into the updated dim.",
            },
            "evidence": {
                "type": "object",
                "description": "Return dict from one of the 5 MVP fetchers.",
            },
            "candidate_profile_summary": {
                "type": "string",
                "maxLength": 4000,
                "description": "Compressed candidate profile for context.",
            },
        },
        "required": [
            "dimension_name",
            "original_score",
            "original_confidence",
            "original_rationale",
            "evidence",
        ],
    },
    return_description=(
        "`{dimension: {name, score, confidence, weight, rationale, "
        "citation: {text, char_offset_start, char_offset_end}}, "
        "evidence_hash}`. `evidence_hash` lets the workflow detect "
        "duplicate rescore attempts on identical evidence."
    ),
)


_MARK_SCORECARD_DONE = ToolSpec(
    name="mark_scorecard_done",
    activity_name="scorecard.mark_scorecard_done",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "TERMINAL TOOL. Signals that evidence gathering is complete and "
        "the workflow may proceed to deterministic post-processing "
        "(compute_overall_match_score → resolve_citations → "
        "persist_scorecard). The activity itself has no side effects — "
        "it is a typed control-flow primitive that ends the LLM loop."
    ),
    when_to_use_md=(
        "Call exactly once, at the end of the run, with one of three "
        "reasons:\n"
        "- 'all_dims_confident': every dim now has confidence >= 0.65.\n"
        "- 'diminishing_returns': remaining low-conf dims have no viable "
        "fetcher (e.g. communication_clarity with no LinkedIn data).\n"
        "- 'budget_imminent': you are close to the 8-tool / 180s / $0.50 "
        "cap and further calls are unlikely to land before exhaustion.\n"
        "The optional `message` lets you leave an operator-visible note."
    ),
    when_not_to_use_md=(
        "Do not call before attempting to rescore at least one "
        "low-confidence dim (unless the initial scorecard was already "
        "fully confident, in which case 'all_dims_confident' is "
        "appropriate). Do not call more than once per run; this "
        "terminates the loop. Do not pass 'budget_exhausted' — that "
        "reason is reserved for the workflow's forced-termination path."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": [
                    "all_dims_confident",
                    "diminishing_returns",
                    "budget_imminent",
                ],
                "description": (
                    "Closed enum. 'budget_exhausted' is reserved for the "
                    "workflow's forced-termination injection and MUST "
                    "NOT be emitted by the LLM."
                ),
            },
            "message": {
                "type": ["string", "null"],
                "maxLength": 500,
                "description": "Optional operator-visible explanation.",
            },
        },
        "required": ["reason"],
    },
    return_description=(
        "`{done: true, reason: str}` — typed sentinel the workflow uses "
        "to break the loop and record the chosen reason on the "
        "ScorecardWorkflowResult."
    ),
)


# Exported in the order the LLM should consider them: planner first, then
# fetchers (cheapest signals first), then rescore, then terminate. This
# ordering also shows up in `render_tools_for_llm` output, so it doubles as
# a soft prompt structure.
SCORECARD_TOOLS: tuple[ToolSpec, ...] = (
    _SELECT_EVIDENCE_SOURCE,
    _FETCH_REPO_READMES,
    _FETCH_COMMIT_HISTORY,
    _FETCH_PR_REVIEW_HISTORY,
    _FETCH_REPO_LANGUAGES,
    _FETCH_USER_ORGS_AND_STARS,
    _RESCORE_DIMENSION,
    _MARK_SCORECARD_DONE,
)


# ---------------------------------------------------------------------------
# 8.2 v2 reserved tool stubs
# ---------------------------------------------------------------------------
# These names are visible to `core.tool_registry.get_tool` so that future v2
# prompts (or accidental v1 mentions) resolve to a typed spec rather than a
# KeyError. They are NOT included in `SCORECARD_TOOLS`, so the v1 LLM prompt
# never advertises them. The activity_name uses a `scorecard.v2.<name>`
# namespace so that — if a workflow somehow dispatches against the stub —
# Temporal will raise an "activity not registered" error rather than colliding
# with any real v1 activity.
#
# `_v2_stub_handler` is the in-process callable that any direct caller (e.g. a
# unit test exercising the registry) gets when they reach for the stub. It
# raises `NotImplementedYet` so callers can distinguish "this tool is reserved
# for a later release" from "this tool name is a typo".


def _v2_stub_handler(*args, **kwargs):
    """Raise `NotImplementedYet` for any v2 reserved tool invocation.

    Shared across all 5 reserved stubs because the failure mode is identical
    — the tool exists in the contract but its backing activity is not yet
    built. Workflows should never dispatch against `scorecard.v2.*` activity
    names in v1; if they do, Temporal will fail with "activity not
    registered" and that failure is intentional.
    """

    raise NotImplementedYet(
        "This scorecard tool is reserved for v2 and is not implemented in "
        "this release. See docs/plans/scorecard_agent_plan.md §8.2."
    )


def _make_v2_stub(
    name: str,
    description_md: str,
    reserved_for: str,
) -> ToolSpec:
    """Build a forward-declared `ToolSpec` for a v2 tool.

    The arg schema is intentionally an empty object — v2 will fix the schema
    when the real activity is wired. Until then the spec exists only to
    reserve the name in the global registry.
    """

    return ToolSpec(
        name=name,
        activity_name=f"scorecard.v2.{name}",
        cost_class=CostClass.SMALL,
        idempotent=True,
        description_md=description_md,
        when_to_use_md=(
            "RESERVED FOR v2. Not callable in v1; "
            f"reserved for {reserved_for}."
        ),
        when_not_to_use_md=(
            "Do not call in v1 — calling will raise `NotImplementedYet`. "
            "v1 prompts should select from the MVP fetchers only."
        ),
        arg_schema={"type": "object", "properties": {}, "required": []},
        return_description=(
            "Not yet implemented. v2 will populate the return schema when "
            "the backing activity is wired."
        ),
    )


_V2_FETCH_BLOG_POSTS = _make_v2_stub(
    name="fetch_blog_posts",
    description_md=(
        "[v2] RSS / personal-site scrape returning the candidate's recent "
        "long-form writing for `system_design_thinking` evidence."
    ),
    reserved_for="`system_design_thinking` dimension",
)

_V2_FETCH_LINKEDIN_RECOMMENDATIONS = _make_v2_stub(
    name="fetch_linkedin_recommendations",
    description_md=(
        "[v2] LinkedIn public-endorsement scrape returning peer "
        "recommendations for `communication_clarity` evidence."
    ),
    reserved_for="`communication_clarity` dimension",
)

_V2_FETCH_CONFERENCE_TALKS = _make_v2_stub(
    name="fetch_conference_talks",
    description_md=(
        "[v2] Conference / YouTube scrape returning the candidate's "
        "recorded talks for `system_design_thinking` evidence."
    ),
    reserved_for="`system_design_thinking` dimension",
)

_V2_FETCH_HUGGINGFACE_CONTRIBUTIONS = _make_v2_stub(
    name="fetch_huggingface_contributions",
    description_md=(
        "[v2] HuggingFace public-profile scrape returning model uploads, "
        "spaces, and dataset contributions for `ml_research_depth` evidence."
    ),
    reserved_for="`ml_research_depth` dimension",
)

_V2_FETCH_ARXIV_PAPERS = _make_v2_stub(
    name="fetch_arxiv_papers",
    description_md=(
        "[v2] arXiv author search returning the candidate's preprints for "
        "`ml_research_depth` evidence."
    ),
    reserved_for="`ml_research_depth` dimension",
)


_V2_RESERVED_TOOLS: tuple[ToolSpec, ...] = (
    _V2_FETCH_BLOG_POSTS,
    _V2_FETCH_LINKEDIN_RECOMMENDATIONS,
    _V2_FETCH_CONFERENCE_TALKS,
    _V2_FETCH_HUGGINGFACE_CONTRIBUTIONS,
    _V2_FETCH_ARXIV_PAPERS,
)


# Map of v2-stub tool name → in-process handler. Exposed so that the
# workflow's tool dispatcher can pre-check `tool_name in V2_STUB_HANDLERS`
# and synthesize a structured `NotImplementedYet` history entry without
# round-tripping through Temporal (the activity does not exist).
V2_STUB_HANDLERS: dict[str, "object"] = {
    spec.name: _v2_stub_handler for spec in _V2_RESERVED_TOOLS
}


# ---------------------------------------------------------------------------
# Register everything into the global core registry.
# ---------------------------------------------------------------------------
# Order: MVP tools first (so duplicate-name collisions surface against the
# canonical specs), then v2 stubs. `register_tool` is idempotent at the
# registry level — re-importing this module overwrites with the same specs.

for _spec in (*SCORECARD_TOOLS, *_V2_RESERVED_TOOLS):
    register_tool(_spec)


__all__ = [
    "SCORECARD_TOOLS",
    "V2_STUB_HANDLERS",
]
