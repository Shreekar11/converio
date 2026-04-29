"""Integration tests for `index_recruiter_to_graph` against real Neo4j.

Mirrors `test_index_candidate_to_graph.py` exactly — uses the shared
`clean_neo4j` fixture for isolation and asserts MERGE-based idempotency.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.neo4j_client import Neo4jClientManager
from app.schemas.enums import (
    CompanyStage,
    RecruitedFundingStage,
    RoleCategory,
    WorkspaceType,
)
from app.schemas.product.recruiter import (
    RecruiterClientItem,
    RecruiterPlacementItem,
    RecruiterProfile,
)
from app.temporal.product.recruiter_indexing.activities.index_recruiter_to_graph import (
    index_recruiter_to_graph,
)


pytestmark = pytest.mark.usefixtures("clean_neo4j")


def _profile_dict(**overrides) -> dict:
    base = dict(
        recruiter_id=str(uuid.uuid4()),
        full_name="Pat Recruiter",
        email="pat@example.com",
        domain_expertise=[RoleCategory.ENGINEERING, RoleCategory.DATA],
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SERIES_A,
        past_clients=[
            RecruiterClientItem(client_company_name="Stripe", role_focus=["backend"])
        ],
        past_placements=[
            RecruiterPlacementItem(
                candidate_name="Alice",
                company_name="Stripe",
                company_stage=CompanyStage.SERIES_A,
                role_title="SWE",
            )
        ],
    )
    base.update(overrides)
    return RecruiterProfile(**base).model_dump(mode="json")


def _metrics_dict(**overrides) -> dict:
    base = dict(
        fill_rate_pct=80.0,
        avg_days_to_close=25,
        total_placements=4,
        placements_by_stage={"series_a": 3, "seed": 1},
    )
    base.update(overrides)
    return base


async def _count_nodes(label: str) -> int:
    async with await Neo4jClientManager.get_session() as session:
        result = await session.run(f"MATCH (n:{label}) RETURN count(n) AS c")
        record = await result.single()
        return record["c"]


async def _count_relationships(rel_type: str) -> int:
    async with await Neo4jClientManager.get_session() as session:
        result = await session.run(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS c")
        record = await result.single()
        return record["c"]


async def test_happy_path_full_graph_shape():
    recruiter_id = str(uuid.uuid4())

    out = await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(),
    )

    assert out["nodes_merged"] > 0
    assert out["edges_merged"] > 0

    # Recruiter, 2 Domains, 2 CompanyStages, 1 Metric.
    assert await _count_nodes("Recruiter") == 1
    assert await _count_nodes("Domain") == 2
    assert await _count_nodes("CompanyStage") == 2
    assert await _count_nodes("Metric") == 1

    # Edges: 2 EXPERTISE_IN + 2 PLACED_AT + 1 FILL_RATE = 5
    assert await _count_relationships("EXPERTISE_IN") == 2
    assert await _count_relationships("PLACED_AT") == 2
    assert await _count_relationships("FILL_RATE") == 1


async def test_idempotent_rerun_does_not_duplicate_edges_or_nodes():
    """Calling twice with identical input must not multiply nodes or edges."""
    recruiter_id = str(uuid.uuid4())

    await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(),
    )

    counts_after_first = {
        "Recruiter": await _count_nodes("Recruiter"),
        "Domain": await _count_nodes("Domain"),
        "CompanyStage": await _count_nodes("CompanyStage"),
        "Metric": await _count_nodes("Metric"),
        "EXPERTISE_IN": await _count_relationships("EXPERTISE_IN"),
        "PLACED_AT": await _count_relationships("PLACED_AT"),
        "FILL_RATE": await _count_relationships("FILL_RATE"),
    }

    await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(),
    )

    counts_after_second = {
        "Recruiter": await _count_nodes("Recruiter"),
        "Domain": await _count_nodes("Domain"),
        "CompanyStage": await _count_nodes("CompanyStage"),
        "Metric": await _count_nodes("Metric"),
        "EXPERTISE_IN": await _count_relationships("EXPERTISE_IN"),
        "PLACED_AT": await _count_relationships("PLACED_AT"),
        "FILL_RATE": await _count_relationships("FILL_RATE"),
    }

    assert counts_after_first == counts_after_second


async def test_unknown_company_stage_in_metrics_skipped():
    """`placements_by_stage` keys outside CompanyStage enum are skipped (defensive)."""
    recruiter_id = str(uuid.uuid4())

    await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(
            placements_by_stage={"series_a": 1, "totally_made_up_stage": 5}
        ),
    )

    # Only the valid stage is materialized.
    assert await _count_nodes("CompanyStage") == 1
    assert await _count_relationships("PLACED_AT") == 1


async def test_null_fill_rate_pct_skips_fill_rate_edge():
    recruiter_id = str(uuid.uuid4())

    await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(fill_rate_pct=None),
    )

    assert await _count_nodes("Metric") == 0
    assert await _count_relationships("FILL_RATE") == 0


async def test_domain_values_constrained_to_role_category_enum():
    """Pydantic constrains domain values at activity entry — graph contains only enum values.

    Sanity: every Domain node `name` lies in the RoleCategory enum value set.
    """
    recruiter_id = str(uuid.uuid4())
    await index_recruiter_to_graph(
        recruiter_id=recruiter_id,
        profile_data=_profile_dict(),
        metrics_data=_metrics_dict(),
    )

    valid_values = {c.value for c in RoleCategory}
    async with await Neo4jClientManager.get_session() as session:
        result = await session.run("MATCH (d:Domain) RETURN d.name AS name")
        records = [r["name"] async for r in result]

    assert records, "expected at least one Domain node"
    assert set(records).issubset(valid_values)


async def test_two_recruiters_share_same_domain_node():
    """MERGE-based: shared Domain node across recruiters, separate EXPERTISE_IN edges."""
    rec_a = str(uuid.uuid4())
    rec_b = str(uuid.uuid4())

    await index_recruiter_to_graph(
        recruiter_id=rec_a,
        profile_data=_profile_dict(
            domain_expertise=[RoleCategory.ENGINEERING],
            past_clients=[],
            past_placements=[],
        ),
        metrics_data=_metrics_dict(
            placements_by_stage={}, fill_rate_pct=None, avg_days_to_close=None
        ),
    )
    await index_recruiter_to_graph(
        recruiter_id=rec_b,
        profile_data=_profile_dict(
            domain_expertise=[RoleCategory.ENGINEERING],
            past_clients=[],
            past_placements=[],
        ),
        metrics_data=_metrics_dict(
            placements_by_stage={}, fill_rate_pct=None, avg_days_to_close=None
        ),
    )

    assert await _count_nodes("Recruiter") == 2
    assert await _count_nodes("Domain") == 1
    assert await _count_relationships("EXPERTISE_IN") == 2
