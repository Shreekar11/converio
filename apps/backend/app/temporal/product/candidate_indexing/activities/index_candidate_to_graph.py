from temporalio import activity

from app.core.neo4j_client import Neo4jClientManager
from app.schemas.product.candidate import CandidateProfile, GitHubSignals
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


@ActivityRegistry.register("candidate_indexing", "index_candidate_to_graph")
@activity.defn
async def index_candidate_to_graph(
    candidate_id: str,
    profile_data: dict,
    github_signals_data: dict,
) -> dict:
    profile = CandidateProfile.model_validate(profile_data)
    github = GitHubSignals.model_validate(github_signals_data) if github_signals_data else GitHubSignals()

    nodes_merged = 0
    edges_merged = 0

    async with await Neo4jClientManager.get_session() as session:
        # 1. MERGE Candidate node
        await session.run(
            "MERGE (c:Candidate {id: $id}) SET c.name = $name",
            id=candidate_id,
            name=profile.full_name,
        )
        nodes_merged += 1

        # 2. Work history -> Company nodes + WORKED_AT edges
        for item in profile.work_history:
            await session.run(
                """
                MERGE (co:Company {name: $company_name})
                WITH co
                MATCH (c:Candidate {id: $candidate_id})
                MERGE (c)-[:WORKED_AT {role_title: $role, start_date: $start, end_date: $end}]->(co)
                """,
                company_name=item.company,
                candidate_id=candidate_id,
                role=item.role_title,
                start=item.start_date or "",
                end=item.end_date or "",
            )
            nodes_merged += 1
            edges_merged += 1

        # 3. Skills -> Technology nodes + SKILLED_IN edges
        for skill in profile.skills:
            await session.run(
                """
                MERGE (t:Technology {name: $skill_name})
                WITH t
                MATCH (c:Candidate {id: $candidate_id})
                MERGE (c)-[:SKILLED_IN {depth: $depth}]->(t)
                """,
                skill_name=skill.name,
                candidate_id=candidate_id,
                depth=skill.depth,
            )
            nodes_merged += 1
            edges_merged += 1

        # 4. GitHub profile node + HAS_GITHUB edge
        if profile.github_username:
            await session.run(
                """
                MERGE (g:GitHubProfile {username: $username})
                SET g.repo_count = $repo_count,
                    g.top_language = $top_language,
                    g.commits_12m = $commits_12m,
                    g.stars_total = $stars_total
                WITH g
                MATCH (c:Candidate {id: $candidate_id})
                MERGE (c)-[:HAS_GITHUB]->(g)
                """,
                username=profile.github_username,
                repo_count=github.repo_count,
                top_language=github.top_language or "",
                commits_12m=github.commits_12m,
                stars_total=github.stars_total,
                candidate_id=candidate_id,
            )
            nodes_merged += 1
            edges_merged += 1

        # 5. Seniority enum node + SENIORITY edge
        if profile.seniority:
            await session.run(
                """
                MERGE (s:SeniorityEnum {level: $level})
                WITH s
                MATCH (c:Candidate {id: $candidate_id})
                MERGE (c)-[:SENIORITY]->(s)
                """,
                level=profile.seniority,
                candidate_id=candidate_id,
            )
            nodes_merged += 1
            edges_merged += 1

        # 6. Stage fit enum nodes + FITS_STAGE edges
        for stage in (profile.stage_fit or []):
            await session.run(
                """
                MERGE (st:StageEnum {stage: $stage})
                WITH st
                MATCH (c:Candidate {id: $candidate_id})
                MERGE (c)-[:FITS_STAGE]->(st)
                """,
                stage=stage,
                candidate_id=candidate_id,
            )
            nodes_merged += 1
            edges_merged += 1

    LOGGER.info(
        "Candidate indexed to graph",
        extra={
            "candidate_id": candidate_id,
            "nodes_merged": nodes_merged,
            "edges_merged": edges_merged,
        },
    )

    return {"nodes_merged": nodes_merged, "edges_merged": edges_merged}
