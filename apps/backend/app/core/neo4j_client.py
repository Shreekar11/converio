
from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class Neo4jClientManager:
    _driver: AsyncDriver | None = None

    # Global entities — unique by id alone (exist across all workflows/jobs)
    GLOBAL_LABELS = [
        "Candidate", "Company", "Technology", "GitHubProfile",
        "StageEnum", "SeniorityEnum",
        "Recruiter", "Domain", "CompanyStage", "Metric",
    ]

    # Workflow-scoped entities — unique by (id, workflow_id) pair
    WORKFLOW_SCOPED_LABELS = [
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
            # Drop ALL existing constraints first (clean slate for idempotency)
            result = await session.run("SHOW CONSTRAINTS")
            existing = await result.data()
            for rec in existing:
                name = rec.get("name")
                if name and name.startswith("constraint_"):
                    await session.run(f"DROP CONSTRAINT {name} IF EXISTS")

            # Create single-property unique constraints for global labels
            for label in cls.GLOBAL_LABELS:
                name = f"constraint_{label.lower()}_id_unique"
                cypher = (
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                )
                await session.run(cypher)
                LOGGER.info(f"Ensured constraint: {name}")

            # Create composite unique constraints for workflow-scoped labels
            for label in cls.WORKFLOW_SCOPED_LABELS:
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
