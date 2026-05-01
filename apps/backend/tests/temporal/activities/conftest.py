"""Fixtures for candidate-indexing activity tests.

Activities create their own sessions via `app.core.database.async_session_maker`,
which connects to `settings.database_url` — the same engine the parent conftest
uses to create the schema. We therefore truncate via a fresh session on the
shared engine before each test that needs a clean candidates table.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import delete

from app.core.database import async_session_maker
from app.core.neo4j_client import Neo4jClientManager
from app.database.models import (
    Candidate,
    Recruiter,
    RecruiterClient,
    RecruiterPlacement,
)


@pytest_asyncio.fixture
async def truncate_candidates(db_engine):  # noqa: ARG001 — depend on schema setup
    """Delete all rows from candidates before each test."""
    async with async_session_maker() as sess:
        await sess.execute(delete(Candidate))
        await sess.commit()
    yield
    async with async_session_maker() as sess:
        await sess.execute(delete(Candidate))
        await sess.commit()


@pytest_asyncio.fixture
async def truncate_recruiters(db_engine):  # noqa: ARG001 — depend on schema setup
    """Delete all rows from recruiter tables (placements + clients + recruiters) before each test.

    Order matters because of FKs: placements / clients reference recruiters.
    """

    async def _wipe() -> None:
        async with async_session_maker() as sess:
            await sess.execute(delete(RecruiterPlacement))
            await sess.execute(delete(RecruiterClient))
            await sess.execute(delete(Recruiter))
            await sess.commit()

    await _wipe()
    yield
    await _wipe()


@pytest_asyncio.fixture
async def clean_neo4j():
    """Wipe all Neo4j nodes and relationships before each test."""
    async with await Neo4jClientManager.get_session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    yield
    async with await Neo4jClientManager.get_session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
