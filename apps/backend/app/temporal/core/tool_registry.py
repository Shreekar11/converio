"""Tool registry for the augmented LLM primitive.

Provides a typed catalog of tools available to each agent's LLM decision loop.
Each ToolSpec describes one tool the LLM can call: its name, which Temporal
activity backs it, input/output Pydantic schemas, cost class, idempotency,
and the documentation strings the LLM sees when deciding which tool to invoke.

Usage:
    from app.temporal.core.tool_registry import get_tool, render_tools_for_llm, RECRUITER_ASSIGNMENT_TOOLS

    # Get a specific tool spec
    spec = get_tool("search_recruiter_pool")

    # Render tool catalog for inclusion in LLM prompt
    tool_docs = render_tools_for_llm(RECRUITER_ASSIGNMENT_TOOLS)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class CostClass(StrEnum):
    """Approximate per-call cost class used by the LLM for budget reasoning.

    Cost expectations (approximate USD per call):
        FREE   ~ $0       (pure DB / Cypher / deterministic compute)
        MICRO  ~ $0.001   (small embedding lookup or tiny LLM call)
        SMALL  ~ $0.01    (short structured LLM call, single record)
        MEDIUM ~ $0.05    (batched LLM scoring or multi-record reasoning)
    """

    FREE = "free"
    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"


@dataclass(frozen=True)
class ToolSpec:
    """Declarative description of a single tool exposed to the agent's LLM loop.

    The LLM sees `description_md`, `when_to_use_md`, `when_not_to_use_md`,
    `arg_schema`, and `return_description` rendered as Markdown. The runtime
    uses `name` to dispatch a tool call to the corresponding Temporal activity
    via `activity_name`.
    """

    name: str
    activity_name: str
    cost_class: CostClass
    idempotent: bool
    description_md: str
    when_to_use_md: str
    when_not_to_use_md: str
    arg_schema: dict
    return_description: str


_TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    """Register a ToolSpec into the module-level registry, keyed by `spec.name`.

    Idempotent at the registry level: re-registering the same name overwrites
    the prior entry. We log at DEBUG to surface accidental duplicate registration
    during worker bootstrap.
    """

    if spec.name in _TOOL_REGISTRY:
        LOGGER.debug("Overwriting existing tool registration: %s", spec.name)
    _TOOL_REGISTRY[spec.name] = spec


def get_tool(name: str) -> ToolSpec:
    """Return the ToolSpec for `name`, or raise KeyError with a clear message."""

    try:
        return _TOOL_REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_TOOL_REGISTRY)) or "<empty>"
        raise KeyError(
            f"Unknown tool '{name}'. Registered tools: {known}"
        ) from exc


def render_tools_for_llm(specs: Iterable[ToolSpec]) -> str:
    """Render an iterable of ToolSpecs as a single Markdown catalog string.

    The format is stable and meant to be embedded directly into the agent's
    system prompt. JSON Schema for each tool's arguments is fenced as ```json
    so the LLM can parse argument structure unambiguously.
    """

    sections: list[str] = []
    for spec in specs:
        idempotent_str = "yes" if spec.idempotent else "no"
        arg_schema_json = json.dumps(spec.arg_schema, indent=2)
        section = (
            f"### {spec.name}\n"
            f"Activity: `{spec.activity_name}` | Cost: {spec.cost_class.value} | "
            f"Idempotent: {idempotent_str}\n\n"
            f"{spec.description_md}\n\n"
            f"**When to use:** {spec.when_to_use_md}\n"
            f"**When NOT to use:** {spec.when_not_to_use_md}\n\n"
            f"**Arguments (JSON):**\n"
            f"```json\n{arg_schema_json}\n```\n\n"
            f"**Returns:** {spec.return_description}"
        )
        sections.append(section)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Recruiter Assignment Agent — tool catalog
# ---------------------------------------------------------------------------
# Each ToolSpec below maps 1:1 to a Temporal activity in
# app.temporal.product.recruiter_assignment.activities (registered in Wave 2).
# Argument schemas are hand-written JSON Schema fragments so the LLM sees the
# minimum-viable structure without leaking Pydantic-internal field metadata.


_SEARCH_RECRUITER_POOL = ToolSpec(
    name="search_recruiter_pool",
    activity_name="recruiter_assignment.search_recruiter_pool",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Primary recruiter discovery tool. Runs a Cypher traversal over the "
        "knowledge graph filtering recruiters by `EXPERTISE_IN`->Domain, "
        "`PLACED_AT`->CompanyStage, and `FILL_RATE`->Metric. Excludes recruiters "
        "marked inactive or already at capacity. Returns a deduplicated, "
        "ranked-by-graph-degree set of candidates suitable for downstream scoring."
    ),
    when_to_use_md=(
        "Always your first call when starting a recruiter assignment run. "
        "Use the role's exact `role_category`, `seniority_level`, and `stage_fit` "
        "(if known) to get the tightest, highest-signal pool before any widening."
    ),
    when_not_to_use_md=(
        "Do not call repeatedly with identical arguments — the result is "
        "deterministic for a given graph snapshot. If the pool is too small, "
        "call `widen_domain_search` (broader domains) or `relax_stage_match` "
        "(broader stage window) instead of re-querying with the same filters."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "role_category": {
                "type": "string",
                "description": "Canonical role category, e.g. 'engineering', 'gtm', 'design', 'ops', 'data'.",
            },
            "seniority_level": {
                "type": "string",
                "description": "Seniority bucket, e.g. 'junior', 'mid', 'senior', 'staff', 'principal'.",
            },
            "stage_fit": {
                "type": ["string", "null"],
                "description": "Target company stage (e.g. 'seed', 'series_a'). Pass null when stage is unconstrained.",
            },
            "must_have_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hard-required skills/tags. Recruiters lacking any of these are excluded.",
            },
        },
        "required": ["role_category", "seniority_level", "must_have_skills"],
    },
    return_description=(
        "List of RecruiterCandidate summaries: "
        "`[{recruiter_id, display_name, primary_domains, stage_focus, "
        "fill_rate, avg_days_to_close, current_open_roles, capacity_max}]`."
    ),
)


_WIDEN_DOMAIN_SEARCH = ToolSpec(
    name="widen_domain_search",
    activity_name="recruiter_assignment.widen_domain_search",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Expands the recruiter search to adjacent domains in the domain "
        "ontology graph (e.g. engineering -> devops -> sre, or gtm -> "
        "growth -> revops). Walks `ADJACENT_TO` edges up to `adjacency_hops` "
        "steps and returns recruiters with expertise in any reachable domain."
    ),
    when_to_use_md=(
        "Call after `search_recruiter_pool` returns fewer than 5 viable "
        "candidates. Start with `adjacency_hops=1`; only escalate to 2 or 3 "
        "if the 1-hop expansion is still thin."
    ),
    when_not_to_use_md=(
        "Do not call before `search_recruiter_pool` — adjacent-domain matches "
        "should always be considered after exact-domain matches, never as a "
        "first-line search. Do not call with `adjacency_hops > 3`; deeper hops "
        "produce noisy, low-signal matches."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "role_category": {"type": "string"},
            "seniority_level": {"type": "string"},
            "stage_fit": {"type": ["string", "null"]},
            "adjacency_hops": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "How many ADJACENT_TO edges to traverse from the seed domain.",
            },
        },
        "required": ["role_category", "seniority_level", "adjacency_hops"],
    },
    return_description=(
        "List of RecruiterCandidate summaries from adjacent domains, each "
        "annotated with `adjacency_distance` (1, 2, or 3) so downstream scoring "
        "can penalize distance."
    ),
)


_RELAX_STAGE_MATCH = ToolSpec(
    name="relax_stage_match",
    activity_name="recruiter_assignment.relax_stage_match",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Broadens stage matching by accepting recruiters whose dominant "
        "company-stage focus is within +/- `stage_window` ordinal positions of "
        "`stage_fit` on the funding-stage ladder (pre_seed -> seed -> series_a "
        "-> series_b -> series_c -> growth -> public)."
    ),
    when_to_use_md=(
        "Call when the pool remains under 5 candidates after both "
        "`search_recruiter_pool` and `widen_domain_search`. Use "
        "`stage_window=1` first; escalate to 2 only if absolutely necessary. "
        "`stage_fit` MUST be non-null."
    ),
    when_not_to_use_md=(
        "Do not call when `stage_fit` is null — there is no stage constraint "
        "to relax in that case. Do not call before exhausting "
        "`search_recruiter_pool` and `widen_domain_search`; relaxing stage "
        "before domain widening typically produces worse fit."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "role_category": {"type": "string"},
            "seniority_level": {"type": "string"},
            "stage_fit": {
                "type": "string",
                "description": "Required and non-null. The stage to relax around.",
            },
            "stage_window": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2,
                "description": "Ordinal stage distance to allow on either side of stage_fit.",
            },
        },
        "required": ["role_category", "seniority_level", "stage_fit", "stage_window"],
    },
    return_description=(
        "List of RecruiterCandidate summaries with relaxed stage filter, each "
        "annotated with `stage_distance` so scoring can apply a penalty."
    ),
)


_QUERY_RECRUITER_CAPACITY = ToolSpec(
    name="query_recruiter_capacity",
    activity_name="recruiter_assignment.query_recruiter_capacity",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Fetches live capacity for a batch of recruiter IDs from Postgres "
        "(authoritative source for `current_open_roles` and `capacity_max`). "
        "Use this to confirm a recruiter is not at capacity right before "
        "proposing — graph data may be slightly stale."
    ),
    when_to_use_md=(
        "Call after scoring and before calling `propose_assignment_set` to "
        "verify the top candidates still have headroom. Batch the recruiter "
        "IDs into a single call rather than one call per recruiter."
    ),
    when_not_to_use_md=(
        "Do not call on recruiters already confirmed `at_capacity=false` "
        "earlier in the same run — capacity is cached within a run. Do not "
        "call on the entire pool; target the small set of finalists you are "
        "actually considering proposing."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "recruiter_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Recruiter UUIDs to check capacity for.",
            }
        },
        "required": ["recruiter_ids"],
    },
    return_description=(
        "List of `{recruiter_id, current_open_roles, capacity_max, "
        "at_capacity}` objects. `at_capacity` is true iff "
        "`current_open_roles >= capacity_max`."
    ),
)


_CHECK_RECENT_PLACEMENTS = ToolSpec(
    name="check_recent_placements",
    activity_name="recruiter_assignment.check_recent_placements",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Returns the recent placement history for a single recruiter within "
        "the trailing `since_days` window. Used as a recency / activity signal "
        "to distinguish dormant recruiters from currently active ones."
    ),
    when_to_use_md=(
        "Call for finalists only — typically the top 3-5 ranked candidates "
        "right before generating narratives. Useful when a recruiter looks "
        "strong on paper but you need to confirm they have shipped placements "
        "recently. A reasonable default is `since_days=90`."
    ),
    when_not_to_use_md=(
        "Do not call for every recruiter in the pool — this is a per-recruiter "
        "lookup and the cost of N calls grows linearly. Restrict to finalists. "
        "Do not call with `since_days < 30` (too noisy) or `> 180` (signal "
        "decays beyond ~6 months)."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "recruiter_id": {"type": "string"},
            "since_days": {
                "type": "integer",
                "minimum": 30,
                "maximum": 180,
                "description": "Trailing window in days for the placement history.",
            },
        },
        "required": ["recruiter_id", "since_days"],
    },
    return_description=(
        "List of placement records `[{role_title, company_stage, placed_at, "
        "days_to_close}]`, ordered by `placed_at` descending. Empty list if "
        "the recruiter has no placements in the window."
    ),
)


_SUMMARIZE_RECRUITER_TRACK_RECORD = ToolSpec(
    name="summarize_recruiter_track_record",
    activity_name="recruiter_assignment.summarize_recruiter_track_record",
    cost_class=CostClass.SMALL,
    idempotent=True,
    description_md=(
        "Generates a 2-3 sentence narrative summarizing a recruiter's track "
        "record (placements, dominant stages, average time-to-close, notable "
        "wins). The output is rendered verbatim in the operator-facing "
        "proposal UI; keep call volume low."
    ),
    when_to_use_md=(
        "Call only for the final proposed set (top 3-5 recruiters), "
        "immediately before `format_recruiter_recommendations`. Treat this as "
        "an end-of-pipeline polish step."
    ),
    when_not_to_use_md=(
        "Do not generate narratives for every candidate in the pool — this is "
        "a small LLM call and the cost compounds quickly. Do not call before "
        "ranking is finalized; you may end up generating narratives for "
        "recruiters that never make the final cut."
    ),
    arg_schema={
        "type": "object",
        "properties": {"recruiter_id": {"type": "string"}},
        "required": ["recruiter_id"],
    },
    return_description=(
        "`{narrative: string}` — a 2-3 sentence operator-facing summary of "
        "the recruiter's recent track record, written in plain prose."
    ),
)


_SCORE_RECRUITER_FIT = ToolSpec(
    name="score_recruiter_fit",
    activity_name="recruiter_assignment.score_recruiter_fit",
    cost_class=CostClass.MEDIUM,
    idempotent=False,
    description_md=(
        "Batched LLM-based structured scoring of recruiter fit. For each "
        "recruiter ID, the model emits a 0-1 overall score with confidence "
        "and a breakdown across five sub-dimensions: domain, stage, "
        "seniority, fill_rate, close_time. Scores feed deterministic ranking."
    ),
    when_to_use_md=(
        "Call once you have a candidate pool from `search_recruiter_pool` "
        "(plus any widening). Batch up to 10 recruiters per call to amortize "
        "model overhead. Pass the same role context that was used for search."
    ),
    when_not_to_use_md=(
        "Do not re-score recruiters already scored in this run — scores are "
        "memoized for the duration of a workflow execution. Do not call with "
        "an empty `recruiter_ids` list. Do not call before you have a pool."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "recruiter_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
                "description": "Up to 10 recruiter IDs per call.",
            },
            "role_category": {"type": "string"},
            "seniority_level": {"type": "string"},
            "stage_fit": {"type": ["string", "null"]},
            "must_have_skills": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "recruiter_ids",
            "role_category",
            "seniority_level",
            "must_have_skills",
        ],
    },
    return_description=(
        "List of `{recruiter_id, score, confidence, rationale, "
        "sub_scores: {domain, stage, seniority, fill_rate, close_time}}` "
        "objects. `score` and each sub-score are floats in [0, 1]."
    ),
)


_RANK_AND_SELECT_RECRUITERS = ToolSpec(
    name="rank_and_select_recruiters",
    activity_name="recruiter_assignment.rank_and_select_recruiters",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Deterministic weighted ranker. Given recruiter IDs that have already "
        "been scored, computes a weighted composite "
        "(domain*0.35 + fill_rate*0.25 + stage*0.20 + close_time*0.10 + "
        "seniority*0.10) and returns the top `top_n` ordered by composite, "
        "ties broken by confidence."
    ),
    when_to_use_md=(
        "Call after `score_recruiter_fit` has produced scores for every "
        "candidate you want to consider. Choose `top_n` based on how many "
        "finalists you want to propose to the operator (typically 3-5)."
    ),
    when_not_to_use_md=(
        "Do not call before scoring — recruiters lacking sub_scores will be "
        "skipped or rank-zero. Do not pass `top_n` larger than the number of "
        "scored recruiters; the call will simply return all of them, which "
        "wastes a round trip."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "scored_recruiter_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["scored_recruiter_ids", "top_n"],
    },
    return_description=(
        "Ordered list of `{recruiter_id, weighted_score}` (highest first), "
        "truncated to `top_n` entries."
    ),
)


_FORMAT_RECRUITER_RECOMMENDATIONS = ToolSpec(
    name="format_recruiter_recommendations",
    activity_name="recruiter_assignment.format_recruiter_recommendations",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "Builds the structured operator proposal payload by joining ranked "
        "recruiters with their sub-scores, narratives, recent placements, "
        "and capacity. Output matches the operator review UI contract."
    ),
    when_to_use_md=(
        "Call once, after ranking is complete and after narratives have been "
        "generated for the finalists, immediately before "
        "`propose_assignment_set`."
    ),
    when_not_to_use_md=(
        "Do not call multiple times — build the payload once, then propose. "
        "Do not call before scoring, ranking, and narrative generation are "
        "all complete; the payload will be missing required fields."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "ranked_recruiter_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Recruiter IDs in final ranked order.",
            }
        },
        "required": ["ranked_recruiter_ids"],
    },
    return_description=(
        "Operator proposal payload dict: "
        "`{recommendations: [{recruiter_id, display_name, weighted_score, "
        "sub_scores, narrative, recent_placements, capacity}], "
        "stats: {pool_size, scored_count, widened, stage_relaxed}}`."
    ),
)


_PROPOSE_ASSIGNMENT_SET = ToolSpec(
    name="propose_assignment_set",
    activity_name="recruiter_assignment.propose_assignment_set",
    cost_class=CostClass.FREE,
    idempotent=True,
    description_md=(
        "TERMINAL TOOL. Submits the final ranked recruiter proposal for "
        "operator review and exits the agent's search loop. Persists "
        "`AssignmentRecommendation` rows and emits the HITL signal that "
        "pauses the workflow until an operator approves or rejects."
    ),
    when_to_use_md=(
        "Call exactly once, when you are satisfied with the proposed pool "
        "and have a formatted recommendations payload. Set "
        "`quality_flag='high'` for a strong unambiguous match, `'medium'` "
        "for a reasonable but not standout match, and `'low'` when the pool "
        "was thin or the budget forced an early exit."
    ),
    when_not_to_use_md=(
        "Do not call before scoring and ranking — proposing without scores "
        "is a hard error. Do not call more than once per run; this terminates "
        "the loop. Do not call to 'preview' results; once invoked, the "
        "workflow advances to operator review."
    ),
    arg_schema={
        "type": "object",
        "properties": {
            "recruiter_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Final recommended recruiter IDs in ranked order.",
            },
            "rationale": {
                "type": "string",
                "description": "Short prose rationale shown to the operator (why this set).",
            },
            "quality_flag": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Self-assessed quality of the proposed set.",
            },
        },
        "required": ["recruiter_ids", "rationale", "quality_flag"],
    },
    return_description="`{proposed: true}` once the proposal is durably persisted.",
)


for _spec in (
    _SEARCH_RECRUITER_POOL,
    _WIDEN_DOMAIN_SEARCH,
    _RELAX_STAGE_MATCH,
    _QUERY_RECRUITER_CAPACITY,
    _CHECK_RECENT_PLACEMENTS,
    _SUMMARIZE_RECRUITER_TRACK_RECORD,
    _SCORE_RECRUITER_FIT,
    _RANK_AND_SELECT_RECRUITERS,
    _FORMAT_RECRUITER_RECOMMENDATIONS,
    _PROPOSE_ASSIGNMENT_SET,
):
    register_tool(_spec)


RECRUITER_ASSIGNMENT_TOOLS: tuple[ToolSpec, ...] = tuple(_TOOL_REGISTRY.values())


__all__ = [
    "CostClass",
    "ToolSpec",
    "register_tool",
    "get_tool",
    "render_tools_for_llm",
    "RECRUITER_ASSIGNMENT_TOOLS",
]
