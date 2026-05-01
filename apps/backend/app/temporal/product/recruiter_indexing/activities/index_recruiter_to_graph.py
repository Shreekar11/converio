from temporalio import activity

from app.core.neo4j_client import Neo4jClientManager
from app.schemas.enums import CompanyStage, RoleCategory
from app.schemas.product.recruiter import ComputedMetrics, RecruiterProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Pre-compute enum value sets for runtime defensive validation.
_ROLE_CATEGORY_VALUES: set[str] = {c.value for c in RoleCategory}
_COMPANY_STAGE_VALUES: set[str] = {s.value for s in CompanyStage}


@ActivityRegistry.register("recruiter_indexing", "index_recruiter_to_graph")
@activity.defn
async def index_recruiter_to_graph(
    recruiter_id: str,
    profile_data: dict,
    metrics_data: dict,
) -> dict:
    """Idempotent Neo4j MERGE pipeline for the recruiter side of the graph.

    All writes are MERGE-based and parameterized — replay-safe and SQLi-equivalent-safe.
    Edge `level` for EXPERTISE_IN is intentionally omitted in v1 (no level signal from
    wizard). Adding it later requires either a property-on-MERGE or `ON CREATE SET`
    pattern so the edge identity stays `(Recruiter, Domain)` and re-runs stay idempotent.
    """
    profile = RecruiterProfile.model_validate(profile_data)
    metrics = ComputedMetrics.model_validate(metrics_data)

    nodes_merged = 0
    edges_merged = 0

    # Cypher SET cannot accept null without `coalesce`; coerce optional enums to "" for v1.
    workspace_type = profile.workspace_type.value if profile.workspace_type else ""
    recruited_funding_stage = (
        profile.recruited_funding_stage.value if profile.recruited_funding_stage else ""
    )

    async with await Neo4jClientManager.get_session() as session:
        # 1. Recruiter node — always merged.
        await session.run(
            """
            MERGE (r:Recruiter {id: $recruiter_id})
            SET r.full_name = $full_name,
                r.workspace_type = $workspace_type,
                r.recruited_funding_stage = $recruited_funding_stage
            """,
            recruiter_id=recruiter_id,
            full_name=profile.full_name,
            workspace_type=workspace_type,
            recruited_funding_stage=recruited_funding_stage,
        )
        nodes_merged += 1

        # 2. Domain expertise → EXPERTISE_IN edges. Reject values outside RoleCategory.
        for domain in profile.domain_expertise:
            domain_value = domain.value
            if domain_value not in _ROLE_CATEGORY_VALUES:
                LOGGER.warning(
                    "Skipping unknown domain value for graph indexing",
                    extra={"recruiter_id": recruiter_id, "domain": domain_value},
                )
                continue
            await session.run(
                """
                MERGE (d:Domain {name: $domain})
                WITH d
                MATCH (r:Recruiter {id: $recruiter_id})
                MERGE (r)-[:EXPERTISE_IN]->(d)
                """,
                domain=domain_value,
                recruiter_id=recruiter_id,
            )
            nodes_merged += 1
            edges_merged += 1

        # 3. PLACED_AT edges per company stage with rolling counts.
        avg_days_to_close = metrics.avg_days_to_close  # may be None
        for stage, count in metrics.placements_by_stage.items():
            if stage not in _COMPANY_STAGE_VALUES:
                LOGGER.warning(
                    "Skipping unknown company_stage for graph indexing",
                    extra={"recruiter_id": recruiter_id, "stage": stage},
                )
                continue

            if avg_days_to_close is not None:
                await session.run(
                    """
                    MERGE (cs:CompanyStage {stage: $stage})
                    WITH cs
                    MATCH (r:Recruiter {id: $recruiter_id})
                    MERGE (r)-[rel:PLACED_AT]->(cs)
                    SET rel.count = $count,
                        rel.avg_days_to_close = $avg_days_to_close
                    """,
                    stage=stage,
                    recruiter_id=recruiter_id,
                    count=count,
                    avg_days_to_close=avg_days_to_close,
                )
            else:
                await session.run(
                    """
                    MERGE (cs:CompanyStage {stage: $stage})
                    WITH cs
                    MATCH (r:Recruiter {id: $recruiter_id})
                    MERGE (r)-[rel:PLACED_AT]->(cs)
                    SET rel.count = $count
                    """,
                    stage=stage,
                    recruiter_id=recruiter_id,
                    count=count,
                )
            nodes_merged += 1
            edges_merged += 1

        # 4. Fill-rate metric node + FILL_RATE edge (only when fill_rate_pct present).
        if metrics.fill_rate_pct is not None:
            await session.run(
                """
                MERGE (m:Metric {kind: "fill_rate"})
                WITH m
                MATCH (r:Recruiter {id: $recruiter_id})
                MERGE (r)-[rel:FILL_RATE]->(m)
                SET rel.rate_pct = $rate_pct,
                    rel.total_roles = $total_roles
                """,
                recruiter_id=recruiter_id,
                rate_pct=float(metrics.fill_rate_pct),
                total_roles=metrics.total_placements,
            )
            nodes_merged += 1
            edges_merged += 1

    LOGGER.info(
        "Recruiter indexed to graph",
        extra={
            "recruiter_id": recruiter_id,
            "nodes_merged": nodes_merged,
            "edges_merged": edges_merged,
        },
    )

    return {"nodes_merged": nodes_merged, "edges_merged": edges_merged}
