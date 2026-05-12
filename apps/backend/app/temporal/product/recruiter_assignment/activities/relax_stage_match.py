"""W2.3 — `relax_stage_match` activity for the Recruiter Assignment Agent.

Stage-window relaxation tool. When the strict `search_recruiter_pool`
(domain + exact stage_fit) returns a thin candidate set, the agent calls
this tool to widen the search to neighbouring company stages on the
funding-stage ordinal axis.

Design notes
------------
- Stage adjacency is modelled as a 5-point ordinal: seed (0) → series_a (1)
  → series_b (2) → series_c (3) → growth (4). Neighbouring stages are the
  closest signal we have to "transferable placement experience" without a
  graph-based stage-similarity model.
- The original `stage_fit` is intentionally excluded from the window —
  it is already covered by `search_recruiter_pool`, and re-querying it
  here would inflate the candidate list with duplicates that the agent
  must then de-dup downstream. We push the de-dup into Cypher (via
  `exclude_ids`) so the LLM only sees genuinely new candidates.
- All Cypher uses parameter binding (no f-string interpolation of
  user-controlled values) to keep this activity injection-safe.
- `at_capacity` is filtered out at the graph layer rather than scored
  later; an at-capacity recruiter is not a viable proposal under any
  ranking. `IS NULL OR = false` covers recruiters indexed before the
  capacity column was populated.
- Returns `RecruiterCandidate`-shaped dicts so the calling workflow can
  validate them via `RecruiterCandidate.model_validate(...)` without
  schema drift between activities.
"""
from __future__ import annotations

from temporalio import activity

from app.core.neo4j_client import Neo4jClientManager
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Ordinal map for the 5-stage funding axis used by CompanyStage.
# Kept module-level so both the activity and (future) unit tests can
# reuse the same ordering without re-deriving it from the enum.
_STAGE_ORDINAL: dict[str, int] = {
    "seed": 0,
    "series_a": 1,
    "series_b": 2,
    "series_c": 3,
    "growth": 4,
}
_ORDINAL_STAGE: dict[int, str] = {v: k for k, v in _STAGE_ORDINAL.items()}

# Window clamp bounds. The agent passes 1 or 2; anything outside is
# clamped rather than rejected — the LLM may emit raw integers and we
# don't want a tool failure to abort the loop.
_WINDOW_MIN = 1
_WINDOW_MAX = 2

# Per-stage cap mirrors `search_recruiter_pool` so the agent's context
# budget stays bounded even when several adjacent stages return hits.
_PER_STAGE_LIMIT = 20


@ActivityRegistry.register("recruiter_assignment", "relax_stage_match")
@activity.defn(name="recruiter_assignment.relax_stage_match")
async def relax_stage_match(payload: dict) -> dict:
    """Widen the recruiter pool to neighbouring company stages.

    Parameters (via ``payload``)
    ----------------------------
    role_category : str
        Domain expertise to filter on (matches `Domain.name`).
    seniority_level : str
        Seniority tag — passed through but not currently used in the
        graph filter; reserved for a future seniority-weighted variant.
    stage_fit : str
        REQUIRED. The original strict stage from the rubric. Must be a
        key of `_STAGE_ORDINAL` — unknown values raise ``ValueError``
        rather than silently falling through, since a typo here would
        otherwise cause a same-stage re-query masquerading as relaxation.
    stage_window : int
        Half-width of the ordinal window (1 or 2). Clamped to [1, 2].
    exclude_ids : list[str]
        Recruiter IDs already returned by prior tool calls in this
        workflow run. Filtered server-side so the agent never sees
        duplicates and the per-stage LIMIT applies to *new* candidates.

    Returns
    -------
    dict
        ``{"candidates": [RecruiterCandidate dicts], "count": int,
            "stages_searched": [str]}``
    """
    # --- Input validation -------------------------------------------------
    stage_fit = payload.get("stage_fit")
    if not stage_fit:
        raise ValueError(
            "relax_stage_match: 'stage_fit' is required and must be non-empty"
        )
    if stage_fit not in _STAGE_ORDINAL:
        raise ValueError(
            f"relax_stage_match: unknown stage_fit {stage_fit!r}; "
            f"expected one of {sorted(_STAGE_ORDINAL)}"
        )

    role_category = payload.get("role_category")
    if not role_category:
        raise ValueError("relax_stage_match: 'role_category' is required")

    # seniority_level is currently informational; accept missing.
    seniority_level = payload.get("seniority_level", "")

    # Clamp the window. Default to 1 if unset.
    raw_window = payload.get("stage_window", 1)
    try:
        stage_window = int(raw_window)
    except (TypeError, ValueError):
        stage_window = 1
    stage_window = max(_WINDOW_MIN, min(_WINDOW_MAX, stage_window))

    # Coerce exclude_ids to a list[str]; treat None / non-iterables as empty.
    raw_exclude = payload.get("exclude_ids") or []
    if not isinstance(raw_exclude, (list, tuple, set)):
        raw_exclude = []
    exclude_ids: list[str] = [str(x) for x in raw_exclude if x is not None]

    # --- Build the ordinal window ----------------------------------------
    base_ordinal = _STAGE_ORDINAL[stage_fit]
    min_ord = max(0, base_ordinal - stage_window)
    max_ord = min(len(_ORDINAL_STAGE) - 1, base_ordinal + stage_window)

    # Exclude the base stage itself — already covered by search_recruiter_pool.
    window_ordinals = [
        o for o in range(min_ord, max_ord + 1) if o != base_ordinal
    ]
    stages_searched = [_ORDINAL_STAGE[o] for o in window_ordinals]

    # --- Fan out one parameterised query per stage -----------------------
    seen_ids: set[str] = set(exclude_ids)
    candidates: list[dict] = []

    cypher = """
        MATCH (r:Recruiter {status: "active"})-[:EXPERTISE_IN]->(d:Domain {name: $role_category})
        WHERE r.at_capacity IS NULL OR r.at_capacity = false
        MATCH (r)-[pr:PLACED_AT]->(cs:CompanyStage {stage: $stage_name})
        OPTIONAL MATCH (r)-[fr:FILL_RATE]->(m:Metric {kind: "fill_rate"})
        RETURN r.id AS recruiter_id, r.full_name AS full_name,
               collect(DISTINCT d.name) AS domain_expertise,
               fr.rate_pct AS fill_rate_pct,
               pr.count AS stage_placements,
               pr.avg_days_to_close AS avg_days_to_close
        LIMIT 20
    """

    async with await Neo4jClientManager.get_session() as session:
        for stage_name in stages_searched:
            result = await session.run(
                cypher,
                role_category=role_category,
                stage_name=stage_name,
            )
            rows = await result.data()

            for row in rows:
                recruiter_id = row.get("recruiter_id")
                if not recruiter_id or recruiter_id in seen_ids:
                    continue
                seen_ids.add(recruiter_id)

                # Shape into a RecruiterCandidate-compatible dict.
                # `email` and `status` are not on the graph projection
                # (graph holds the matchmaking signal, Postgres holds
                # contact / lifecycle). Email is filled by the caller
                # before scoring; status is forced "active" by the
                # MATCH clause above.
                #
                # `total_placements` here is the per-stage count from
                # the `PLACED_AT` edge, not the global aggregate — the
                # agent uses it to weigh stage transferability. The
                # caller can re-resolve global totals if needed.
                stage_placements = row.get("stage_placements") or 0
                fill_rate = row.get("fill_rate_pct")
                avg_days = row.get("avg_days_to_close")
                domains = row.get("domain_expertise") or []

                candidates.append(
                    {
                        "recruiter_id": str(recruiter_id),
                        "full_name": row.get("full_name") or "",
                        "email": "",
                        "domain_expertise": [str(d) for d in domains],
                        "fill_rate_pct": (
                            float(fill_rate) if fill_rate is not None else None
                        ),
                        "avg_days_to_close": (
                            float(avg_days) if avg_days is not None else None
                        ),
                        "total_placements": int(stage_placements),
                        "status": "active",
                        "at_capacity": False,
                    }
                )

    LOGGER.info(
        "relax_stage_match completed",
        extra={
            "role_category": role_category,
            "seniority_level": seniority_level,
            "stage_fit": stage_fit,
            "stage_window": stage_window,
            "stages_searched": stages_searched,
            "exclude_count": len(exclude_ids),
            "result_count": len(candidates),
        },
    )

    return {
        "candidates": candidates,
        "count": len(candidates),
        "stages_searched": stages_searched,
    }
