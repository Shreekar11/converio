"""Integration tests for B5 `index_candidate_to_graph` against real Neo4j."""
from __future__ import annotations

import uuid

import pytest

from app.core.neo4j_client import Neo4jClientManager
from app.schemas.product.candidate import (
    CandidateProfile,
    GitHubSignals,
    Skill,
    WorkHistoryItem,
)
from app.temporal.product.candidate_indexing.activities.index_candidate_to_graph import (
    index_candidate_to_graph,
)


pytestmark = pytest.mark.usefixtures("clean_neo4j")


def _profile_dict(**overrides) -> dict:
    base = dict(
        full_name="Alice Smith",
        seniority="senior",
        github_username="alice-smith",
        stage_fit=["series_a", "series_b"],
        skills=[Skill(name="Python"), Skill(name="FastAPI")],
        work_history=[
            WorkHistoryItem(company="Stripe", role_title="SWE"),
            WorkHistoryItem(company="Google", role_title="Staff"),
        ],
    )
    base.update(overrides)
    return CandidateProfile(**base).model_dump(mode="json")


def _github_dict(**overrides) -> dict:
    base = dict(repo_count=20, top_language="Python", commits_12m=300, stars_total=50)
    base.update(overrides)
    return GitHubSignals(**base).model_dump(mode="json")


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


async def test_candidate_node_created_with_id_and_name():
    candidate_id = str(uuid.uuid4())

    out = await index_candidate_to_graph(
        candidate_id=candidate_id,
        profile_data=_profile_dict(full_name="Alice Smith"),
        github_signals_data=_github_dict(),
    )

    assert out["nodes_merged"] > 0
    assert out["edges_merged"] > 0

    async with await Neo4jClientManager.get_session() as session:
        result = await session.run(
            "MATCH (c:Candidate {id: $id}) RETURN c.name AS name",
            id=candidate_id,
        )
        record = await result.single()

    assert record is not None
    assert record["name"] == "Alice Smith"


async def test_full_graph_shape():
    """Verify all expected node types and edges are created."""
    candidate_id = str(uuid.uuid4())

    await index_candidate_to_graph(
        candidate_id=candidate_id,
        profile_data=_profile_dict(),
        github_signals_data=_github_dict(),
    )

    assert await _count_nodes("Candidate") == 1
    assert await _count_nodes("Company") == 2  # Stripe, Google
    assert await _count_nodes("Technology") == 2  # Python, FastAPI
    assert await _count_nodes("GitHubProfile") == 1
    assert await _count_nodes("SeniorityEnum") == 1
    assert await _count_nodes("StageEnum") == 2  # series_a, series_b

    assert await _count_relationships("WORKED_AT") == 2
    assert await _count_relationships("SKILLED_IN") == 2
    assert await _count_relationships("HAS_GITHUB") == 1
    assert await _count_relationships("SENIORITY") == 1
    assert await _count_relationships("FITS_STAGE") == 2


async def test_idempotent_merge_on_rerun():
    """Calling twice with identical input must not create duplicate nodes/edges."""
    candidate_id = str(uuid.uuid4())

    await index_candidate_to_graph(
        candidate_id=candidate_id,
        profile_data=_profile_dict(),
        github_signals_data=_github_dict(),
    )

    counts_after_first = {
        "Candidate": await _count_nodes("Candidate"),
        "Company": await _count_nodes("Company"),
        "Technology": await _count_nodes("Technology"),
        "GitHubProfile": await _count_nodes("GitHubProfile"),
        "SeniorityEnum": await _count_nodes("SeniorityEnum"),
        "StageEnum": await _count_nodes("StageEnum"),
        "WORKED_AT": await _count_relationships("WORKED_AT"),
        "SKILLED_IN": await _count_relationships("SKILLED_IN"),
        "HAS_GITHUB": await _count_relationships("HAS_GITHUB"),
        "SENIORITY": await _count_relationships("SENIORITY"),
        "FITS_STAGE": await _count_relationships("FITS_STAGE"),
    }

    # Re-run with the same payload
    await index_candidate_to_graph(
        candidate_id=candidate_id,
        profile_data=_profile_dict(),
        github_signals_data=_github_dict(),
    )

    counts_after_second = {
        "Candidate": await _count_nodes("Candidate"),
        "Company": await _count_nodes("Company"),
        "Technology": await _count_nodes("Technology"),
        "GitHubProfile": await _count_nodes("GitHubProfile"),
        "SeniorityEnum": await _count_nodes("SeniorityEnum"),
        "StageEnum": await _count_nodes("StageEnum"),
        "WORKED_AT": await _count_relationships("WORKED_AT"),
        "SKILLED_IN": await _count_relationships("SKILLED_IN"),
        "HAS_GITHUB": await _count_relationships("HAS_GITHUB"),
        "SENIORITY": await _count_relationships("SENIORITY"),
        "FITS_STAGE": await _count_relationships("FITS_STAGE"),
    }

    assert counts_after_first == counts_after_second


async def test_no_github_username_skips_github_node():
    candidate_id = str(uuid.uuid4())

    await index_candidate_to_graph(
        candidate_id=candidate_id,
        profile_data=_profile_dict(github_username=None),
        github_signals_data={},
    )

    assert await _count_nodes("GitHubProfile") == 0
    assert await _count_relationships("HAS_GITHUB") == 0


async def test_two_candidates_share_same_company_node():
    """Both candidates worked at Stripe -> single Company node, two WORKED_AT edges."""
    cand_a = str(uuid.uuid4())
    cand_b = str(uuid.uuid4())

    shared_history = [WorkHistoryItem(company="Stripe", role_title="SWE")]

    await index_candidate_to_graph(
        candidate_id=cand_a,
        profile_data=_profile_dict(
            full_name="Alice",
            github_username=None,
            skills=[],
            stage_fit=[],
            seniority=None,
            work_history=shared_history,
        ),
        github_signals_data={},
    )
    await index_candidate_to_graph(
        candidate_id=cand_b,
        profile_data=_profile_dict(
            full_name="Bob",
            github_username=None,
            skills=[],
            stage_fit=[],
            seniority=None,
            work_history=shared_history,
        ),
        github_signals_data={},
    )

    assert await _count_nodes("Candidate") == 2
    assert await _count_nodes("Company") == 1
    assert await _count_relationships("WORKED_AT") == 2
