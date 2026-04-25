
from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class Neo4jClientManager:
    _driver: AsyncDriver | None = None

    ENTITY_LABELS = [
        # Candidate-side family (context doc §7)
        "Candidate", "Company", "Technology", "GitHubProfile",
        "StageEnum", "SeniorityEnum",
        # Recruiter-side family
        "Recruiter", "Domain", "CompanyStage", "Metric",
        # Cross-family (enables rich GraphRAG queries)
        "Job",
    ]

    @classmethod
    async def get_driver(cls) -> AsyncDriver:
        if cls._driver is None:
            cls._driver = AsyncGraphDatabase.driver(
                settings.neo4j.uri,
                auth=(settings.neo4j.username, settings.neo4j.password),
            )
        return cls._driver

    @classmethod
    async def get_session(cls):
        driver = await cls.get_driver()
        return driver.session()

    @classmethod
    async def ensure_constraints(cls) -> None:
        async with await cls.get_session() as session:
            # Drop legacy single-field constraints
            result = await session.run("SHOW CONSTRAINTS")
            for rec in await result.data():
                if (
                    rec.get("properties")
                    and len(rec["properties"]) == 1
                    and rec["properties"][0] == "id"
                ):
                    await session.run(f"DROP CONSTRAINT {rec['name']} IF EXISTS")

            # Create composite constraints per label
            for label in cls.ENTITY_LABELS:
                name = f"constraint_{label.lower()}_id_workflow_unique"
                cypher = (
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE (n.id, n.workflow_id) IS UNIQUE"
                )
                await session.run(cypher)
                LOGGER.info(f"Ensured constraint: {name}")

    @classmethod
    async def close(cls) -> None:
        if cls._driver is not None:
            await cls._driver.close()
            cls._driver = None
