"""Primary recruiter pool search — Neo4j Cypher traversal over EXPERTISE_IN, PLACED_AT, FILL_RATE edges."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.core.neo4j_client import Neo4jClientManager
from app.database.models import Recruiter
from app.schemas.product.recruiter_assignment import RecruiterCandidate
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


# Cypher templates are static strings — all user values flow through bound
# parameters ($role_category, $stage_fit). No string interpolation of payload
# values, so this is SQLi-equivalent-safe at the Bolt protocol level.
_CYPHER_WITH_STAGE = """
MATCH (r:Recruiter {status: "active"})-[:EXPERTISE_IN]->(d:Domain {name: $role_category})
WHERE r.at_capacity IS NULL OR r.at_capacity = false
OPTIONAL MATCH (r)-[pr:PLACED_AT]->(cs:CompanyStage {stage: $stage_fit})
OPTIONAL MATCH (r)-[fr:FILL_RATE]->(m:Metric {kind: "fill_rate"})
RETURN r.id AS recruiter_id,
       r.full_name AS full_name,
       r.workspace_type AS workspace_type,
       collect(DISTINCT d.name) AS domain_expertise,
       fr.rate_pct AS fill_rate_pct,
       coalesce(pr.count, 0) AS stage_placements,
       pr.avg_days_to_close AS avg_days_to_close
LIMIT 50
"""

_CYPHER_WITHOUT_STAGE = """
MATCH (r:Recruiter {status: "active"})-[:EXPERTISE_IN]->(d:Domain {name: $role_category})
WHERE r.at_capacity IS NULL OR r.at_capacity = false
OPTIONAL MATCH (r)-[fr:FILL_RATE]->(m:Metric {kind: "fill_rate"})
RETURN r.id AS recruiter_id,
       r.full_name AS full_name,
       r.workspace_type AS workspace_type,
       collect(DISTINCT d.name) AS domain_expertise,
       fr.rate_pct AS fill_rate_pct,
       null AS stage_placements,
       null AS avg_days_to_close
LIMIT 50
"""


def _to_float(value: object) -> float | None:
    """Coerce Neo4j/PG numeric (int | float | Decimal | None) to float | None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)  # type: ignore[arg-type]


@ActivityRegistry.register("recruiter_assignment", "search_recruiter_pool")
@activity.defn(name="recruiter_assignment.search_recruiter_pool")
async def search_recruiter_pool(payload: dict) -> dict:
    """Search the active recruiter pool keyed on role_category + optional stage_fit.

    Two-phase lookup:
      1. Neo4j returns the eligible set (active, non-at-capacity recruiters with
         EXPERTISE_IN the target domain), enriched with FILL_RATE and (when
         stage_fit is provided) PLACED_AT edge attributes.
      2. Postgres provides the authoritative `email`, `total_placements`,
         `avg_days_to_close`, and `at_capacity` fields needed by the operator UI
         and downstream scoring — these live in PG, not the graph.

    Empty graph result is not an error — returns `{"candidates": [], "count": 0}`.
    Neo4j / PG errors are allowed to bubble up so Temporal's activity retry policy
    handles transient failures.
    """
    role_category: str = payload["role_category"]
    stage_fit: str | None = payload.get("stage_fit")
    # `seniority_level` and `must_have_skills` accepted in payload contract for
    # forward compatibility with future Cypher refinements; intentionally unused
    # in v1 traversal.
    _ = payload.get("seniority_level")
    _ = payload.get("must_have_skills", [])

    # ---- 1. Neo4j traversal -------------------------------------------------
    cypher = _CYPHER_WITH_STAGE if stage_fit is not None else _CYPHER_WITHOUT_STAGE
    params: dict[str, object] = {"role_category": role_category}
    if stage_fit is not None:
        params["stage_fit"] = stage_fit

    async with await Neo4jClientManager.get_session() as session:
        result = await session.run(cypher, **params)
        graph_records = await result.data()

    if not graph_records:
        LOGGER.info(
            "Recruiter pool search",
            extra={
                "role_category": role_category,
                "stage_fit": stage_fit,
                "count_returned": 0,
            },
        )
        return {"candidates": [], "count": 0}

    # Index graph rows by recruiter_id for the merge step.
    graph_by_id: dict[str, dict] = {}
    recruiter_uuids: list[uuid.UUID] = []
    for rec in graph_records:
        rid = rec.get("recruiter_id")
        if rid is None:
            continue
        try:
            recruiter_uuids.append(uuid.UUID(rid))
        except (ValueError, TypeError):
            LOGGER.warning(
                "Skipping recruiter with non-UUID id from graph",
                extra={"recruiter_id": rid},
            )
            continue
        graph_by_id[rid] = rec

    if not recruiter_uuids:
        LOGGER.info(
            "Recruiter pool search",
            extra={
                "role_category": role_category,
                "stage_fit": stage_fit,
                "count_returned": 0,
            },
        )
        return {"candidates": [], "count": 0}

    # ---- 2. PG enrichment ---------------------------------------------------
    pg_by_id: dict[str, Recruiter] = {}
    async with async_session_maker() as pg_session:
        pg_result = await pg_session.execute(
            select(
                Recruiter.id,
                Recruiter.email,
                Recruiter.total_placements,
                Recruiter.avg_days_to_close,
                Recruiter.at_capacity,
                Recruiter.status,
            ).where(Recruiter.id.in_(recruiter_uuids))
        )
        for row in pg_result.all():
            pg_by_id[str(row.id)] = row  # type: ignore[assignment]

    # ---- 3. Merge into RecruiterCandidate -----------------------------------
    candidates: list[RecruiterCandidate] = []
    for rid, graph_row in graph_by_id.items():
        pg_row = pg_by_id.get(rid)
        if pg_row is None:
            # Graph/PG drift — skip rather than fabricate email. Logged for observability.
            LOGGER.warning(
                "Recruiter present in graph but missing in PG; skipping",
                extra={"recruiter_id": rid},
            )
            continue

        # Defense-in-depth: PG status filter (graph already filters, but the two
        # stores can drift). 404-equivalent: silently drop non-active rows.
        if pg_row.status != "active":
            continue
        if bool(pg_row.at_capacity):
            continue

        # Prefer PG `avg_days_to_close` (authoritative recruiter-level rolling
        # average) over the graph PLACED_AT edge value (per-stage). The edge
        # value is still useful for scoring and is captured upstream in the
        # graph payload — but the candidate summary surfaces the recruiter-level
        # number that operator UIs render.
        pg_avg = _to_float(pg_row.avg_days_to_close)
        graph_avg = _to_float(graph_row.get("avg_days_to_close"))
        merged_avg = pg_avg if pg_avg is not None else graph_avg

        domain_expertise = graph_row.get("domain_expertise") or []
        # Cypher `collect(DISTINCT ...)` ordering is non-deterministic — sort for
        # replay-stable activity output.
        if isinstance(domain_expertise, list):
            domain_expertise = sorted(str(d) for d in domain_expertise if d is not None)

        try:
            candidate = RecruiterCandidate(
                recruiter_id=rid,
                full_name=str(graph_row.get("full_name") or ""),
                email=str(pg_row.email),
                domain_expertise=domain_expertise,
                fill_rate_pct=_to_float(graph_row.get("fill_rate_pct")),
                avg_days_to_close=merged_avg,
                total_placements=int(pg_row.total_placements or 0),
                status=str(pg_row.status),
                at_capacity=bool(pg_row.at_capacity),
            )
        except Exception as exc:  # pragma: no cover — defensive
            LOGGER.warning(
                "Failed to materialize RecruiterCandidate; skipping",
                extra={"recruiter_id": rid, "error": str(exc)},
            )
            continue

        candidates.append(candidate)

    LOGGER.info(
        "Recruiter pool search",
        extra={
            "role_category": role_category,
            "stage_fit": stage_fit,
            "count_returned": len(candidates),
        },
    )

    return {
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "count": len(candidates),
    }
