"""Widen the recruiter pool by searching adjacent domains in the graph.

Used by the Recruiter Assignment Agent (Agent 0) when the primary
`search_recruiter_pool` returns a thin pool. We hop across a small,
hand-curated domain-adjacency graph (engineering <-> data, gtm <-> ops,
etc.) and re-run the same Cypher we use for the primary search, then
deduplicate and exclude IDs the workflow has already seen.

Replay-determinism notes:
  - The activity is purely Neo4j-bound; no clocks, no LLM calls.
  - Adjacency expansion is deterministic (sorted set unioning).
  - The Cypher is parameterized — domain names never get string-interpolated.
"""
from __future__ import annotations

from temporalio import activity

from app.core.neo4j_client import Neo4jClientManager
from app.schemas.enums import RecruiterStatus, RoleCategory
from app.schemas.product.recruiter_assignment import RecruiterCandidate
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Pre-compute enum value set for runtime defensive validation.
_ROLE_CATEGORY_VALUES: set[str] = {c.value for c in RoleCategory}

# Conservative domain adjacency map. Only truly adjacent skill spaces are
# included so we don't dilute fit. `design` has no strong adjacency and is
# intentionally a dead end.
_DOMAIN_ADJACENCY: dict[str, list[str]] = {
    "engineering": ["data", "ops"],
    "data": ["engineering"],
    "gtm": ["ops"],
    "ops": ["gtm", "engineering"],
    "design": [],  # no strong adjacency
}

# Hard ceiling on hop expansion — defensive even though the schema
# already clamps `widen_domain_hops` to [0, 3].
_MAX_HOPS = 3

# Same Cypher template the primary `search_recruiter_pool` uses, with
# `$domain_name` as the only variable. Parameterized — no concatenation.
_WIDEN_CYPHER = """
MATCH (r:Recruiter {status: "active"})-[:EXPERTISE_IN]->(d:Domain {name: $domain_name})
WHERE r.at_capacity IS NULL OR r.at_capacity = false
OPTIONAL MATCH (r)-[fr:FILL_RATE]->(m:Metric {kind: "fill_rate"})
RETURN r.id AS recruiter_id, r.full_name AS full_name,
       collect(DISTINCT d.name) AS domain_expertise,
       fr.rate_pct AS fill_rate_pct
LIMIT 30
"""


def _expand_adjacent_domains(role_category: str, hops: int) -> list[str]:
    """Return the sorted list of adjacent domains to search, given a hop budget.

    - hop 1: direct neighbours of `role_category`
    - hop 2: neighbours-of-neighbours, minus already-visited and minus the
      original `role_category`
    - hop 3: one further BFS layer (rarely useful given map size, but supported)

    Sorted output keeps the activity replay-deterministic regardless of
    Python set-iteration order.
    """
    if role_category not in _DOMAIN_ADJACENCY:
        # Unknown role category — no adjacencies to widen into. Surface as
        # a structured warning so misconfigured payloads are visible in logs.
        LOGGER.warning(
            "widen_domain_search invoked with unknown role_category",
            extra={"role_category": role_category},
        )
        return []

    visited: set[str] = {role_category}
    frontier: set[str] = {role_category}
    collected: set[str] = set()

    for _ in range(min(hops, _MAX_HOPS)):
        next_frontier: set[str] = set()
        for node in frontier:
            for neighbour in _DOMAIN_ADJACENCY.get(node, []):
                if neighbour in visited:
                    continue
                next_frontier.add(neighbour)
                collected.add(neighbour)
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier

    # Always exclude the original role category — caller wants *adjacent* domains.
    collected.discard(role_category)
    return sorted(collected)


def _row_to_candidate(row: dict) -> RecruiterCandidate | None:
    """Map a Cypher row to a RecruiterCandidate, defending against missing fields.

    The widening Cypher only returns a subset of RecruiterCandidate's
    fields; we fill the rest with conservative defaults (active status,
    at_capacity=false matches the WHERE clause, empty email since the
    graph doesn't store PII). The Postgres-side resolver later in the
    workflow can hydrate `email` if needed.
    """
    recruiter_id = row.get("recruiter_id")
    full_name = row.get("full_name")
    if not recruiter_id or full_name is None:
        # Defensive — Neo4j shouldn't return null for these given the MATCH,
        # but skip rather than raise so a stray bad row can't poison the activity.
        LOGGER.warning(
            "widen_domain_search dropping malformed row",
            extra={"row": {k: row.get(k) for k in ("recruiter_id", "full_name")}},
        )
        return None

    domain_expertise = row.get("domain_expertise") or []
    raw_fill_rate = row.get("fill_rate_pct")
    fill_rate_pct: float | None
    if raw_fill_rate is None:
        fill_rate_pct = None
    else:
        try:
            fill_rate_pct = float(raw_fill_rate)
        except (TypeError, ValueError):
            fill_rate_pct = None

    return RecruiterCandidate(
        recruiter_id=str(recruiter_id),
        full_name=str(full_name),
        # Neo4j Recruiter nodes don't carry email; the Postgres recruiter row
        # is the source of truth. Empty string keeps the schema satisfied
        # without inventing data.
        email="",
        domain_expertise=sorted({str(d) for d in domain_expertise if d}),
        fill_rate_pct=fill_rate_pct,
        avg_days_to_close=None,
        total_placements=0,
        # The WHERE clause already filters to active + not-at-capacity, so
        # surface those as the candidate's reported status.
        status=RecruiterStatus.ACTIVE.value,
        at_capacity=False,
    )


@ActivityRegistry.register("recruiter_assignment", "widen_domain_search")
@activity.defn(name="recruiter_assignment.widen_domain_search")
async def widen_domain_search(payload: dict) -> dict:
    """Search adjacent-domain recruiters when the primary pool is thin.

    Payload contract:
      - role_category: str  (RoleCategory value)
      - seniority_level: str
      - stage_fit: str | None
      - adjacency_hops: int  (clamped to [1, 3])
      - exclude_ids: list[str] | None  (recruiter IDs already proposed)

    Returns:
      - candidates: list[RecruiterCandidate dicts]
      - count: int
      - domains_searched: list[str]  (sorted, for deterministic audit)
    """
    role_category = str(payload.get("role_category", ""))
    # `seniority_level` and `stage_fit` are accepted for forward compatibility
    # and audit-trail logging; the current Cypher doesn't filter on them.
    seniority_level = payload.get("seniority_level")
    stage_fit = payload.get("stage_fit")

    # Clamp adjacency_hops to [1, 3] — defensive even though the workflow
    # schema already validates. Treat missing/invalid as 1 (minimum useful hop).
    raw_hops = payload.get("adjacency_hops", 1)
    try:
        hops = int(raw_hops)
    except (TypeError, ValueError):
        hops = 1
    hops = max(1, min(hops, _MAX_HOPS))

    exclude_ids_raw = payload.get("exclude_ids") or []
    exclude_ids: set[str] = {str(rid) for rid in exclude_ids_raw if rid}

    if role_category not in _ROLE_CATEGORY_VALUES:
        LOGGER.warning(
            "widen_domain_search received unknown role_category — returning empty",
            extra={"role_category": role_category},
        )
        return {"candidates": [], "count": 0, "domains_searched": []}

    domains_to_search = _expand_adjacent_domains(role_category, hops)

    if not domains_to_search:
        LOGGER.info(
            "widen_domain_search found no adjacent domains",
            extra={
                "role_category": role_category,
                "adjacency_hops": hops,
                "seniority_level": seniority_level,
                "stage_fit": stage_fit,
            },
        )
        return {"candidates": [], "count": 0, "domains_searched": []}

    seen_ids: set[str] = set(exclude_ids)
    candidates: list[RecruiterCandidate] = []

    async with await Neo4jClientManager.get_session() as session:
        for domain in domains_to_search:
            # Defence in depth — ensure `domain` is a known enum value before
            # we ship it as a parameter. Cypher parameterization already
            # prevents injection; this catches bad data earlier.
            if domain not in _ROLE_CATEGORY_VALUES:
                LOGGER.warning(
                    "widen_domain_search skipping unknown adjacent domain",
                    extra={"domain": domain},
                )
                continue
            result = await session.run(_WIDEN_CYPHER, domain_name=domain)
            rows = await result.data()
            for row in rows:
                candidate = _row_to_candidate(row)
                if candidate is None:
                    continue
                if candidate.recruiter_id in seen_ids:
                    continue
                seen_ids.add(candidate.recruiter_id)
                candidates.append(candidate)

    # Sort the final candidate list by recruiter_id for replay determinism;
    # the agent layer is responsible for ranking.
    candidates.sort(key=lambda c: c.recruiter_id)

    LOGGER.info(
        "widen_domain_search completed",
        extra={
            "role_category": role_category,
            "adjacency_hops": hops,
            "domains_searched": domains_to_search,
            "candidate_count": len(candidates),
            "excluded_count": len(exclude_ids),
        },
    )

    return {
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "count": len(candidates),
        "domains_searched": domains_to_search,
    }
