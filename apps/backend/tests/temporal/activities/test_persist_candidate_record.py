"""Integration tests for B3 `persist_candidate_record` against real PG."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.database.models import Candidate
from app.schemas.product.candidate import CandidateProfile, Skill, WorkHistoryItem
from app.temporal.product.candidate_indexing.activities.persist_candidate_record import (
    compute_dedup_hash,
    persist_candidate_record,
)


pytestmark = pytest.mark.usefixtures("truncate_candidates")


def _profile_dict(**overrides) -> dict:
    base = dict(
        full_name="Alice Smith",
        email="alice@example.com",
        seniority="senior",
        years_experience=8,
        location="SF",
        stage_fit=["series_a"],
        skills=[Skill(name="Python")],
        work_history=[WorkHistoryItem(company="Stripe", role_title="SWE")],
        github_username="alice-smith",
        resume_text="Experienced engineer.",
    )
    base.update(overrides)
    return CandidateProfile(**base).model_dump(mode="json")


def _embedding(value: float = 0.1) -> list[float]:
    return [value] * 384


async def _fetch(candidate_id: str) -> Candidate | None:
    async with async_session_maker() as sess:
        result = await sess.execute(
            select(Candidate).where(Candidate.id == uuid.UUID(candidate_id))
        )
        return result.scalar_one_or_none()


async def test_insert_new_candidate():
    result = await persist_candidate_record(
        profile_data=_profile_dict(),
        embedding=_embedding(),
        github_signals={"repo_count": 10},
        source="recruiter_upload",
        source_recruiter_id=None,
        existing_candidate_id=None,
    )

    assert result["was_insert"] is True
    assert "candidate_id" in result

    row = await _fetch(result["candidate_id"])
    assert row is not None
    assert row.full_name == "Alice Smith"
    assert row.email == "alice@example.com"
    assert row.dedup_hash == compute_dedup_hash("Alice Smith", "alice@example.com")
    assert row.status == "indexing"
    assert row.source == "recruiter_upload"
    assert row.github_signals == {"repo_count": 10}


async def test_upsert_idempotent_when_existing_id_provided():
    """Calling twice with same dedup_hash + existing_candidate_id returns same id and was_insert=False."""
    first = await persist_candidate_record(
        profile_data=_profile_dict(),
        embedding=_embedding(0.1),
        github_signals={"repo_count": 5},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=None,
    )
    candidate_id = first["candidate_id"]

    second = await persist_candidate_record(
        profile_data=_profile_dict(location="NYC"),
        embedding=_embedding(0.2),
        github_signals={"repo_count": 9},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=candidate_id,
    )

    assert second["was_insert"] is False
    assert second["candidate_id"] == candidate_id

    # Only one row exists
    async with async_session_maker() as sess:
        result = await sess.execute(select(Candidate))
        rows = result.scalars().all()
    assert len(rows) == 1

    # Update was applied (location changed; github_signals updated)
    row = await _fetch(candidate_id)
    assert row.location == "NYC"
    assert row.github_signals == {"repo_count": 9}


async def test_existing_id_not_found_falls_back_to_insert():
    """Stale existing_candidate_id should not crash — should insert a fresh row."""
    bogus_id = str(uuid.uuid4())

    result = await persist_candidate_record(
        profile_data=_profile_dict(),
        embedding=_embedding(),
        github_signals={},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=bogus_id,
    )

    assert result["was_insert"] is True
    assert result["candidate_id"] != bogus_id

    row = await _fetch(result["candidate_id"])
    assert row is not None


async def test_embedding_round_trip():
    """A 384-dim float list survives PG (pgvector) round-trip."""
    vec = [round(i * 0.001, 4) for i in range(384)]

    result = await persist_candidate_record(
        profile_data=_profile_dict(),
        embedding=vec,
        github_signals={},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=None,
    )

    row = await _fetch(result["candidate_id"])
    assert row is not None
    assert row.embedding is not None
    # pgvector returns a numpy-like array; compare element-wise as floats.
    stored = list(row.embedding)
    assert len(stored) == 384
    for got, want in zip(stored, vec, strict=True):
        assert float(got) == pytest.approx(want, abs=1e-4)


async def test_empty_embedding_stored_as_null():
    """An empty list embedding is persisted as NULL."""
    result = await persist_candidate_record(
        profile_data=_profile_dict(),
        embedding=[],
        github_signals={},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=None,
    )

    row = await _fetch(result["candidate_id"])
    assert row is not None
    assert row.embedding is None


async def test_jsonb_fields_serialized_correctly():
    """skills/work_history/education round-trip as JSONB."""
    result = await persist_candidate_record(
        profile_data=_profile_dict(
            skills=[Skill(name="Go"), Skill(name="Rust", depth="evidenced_commits")],
            work_history=[
                WorkHistoryItem(company="Google", role_title="Staff", start_date="2020-01-01")
            ],
        ),
        embedding=_embedding(),
        github_signals={},
        source="seed",
        source_recruiter_id=None,
        existing_candidate_id=None,
    )

    row = await _fetch(result["candidate_id"])
    assert row.skills == [
        {"name": "Go", "depth": "claimed_only"},
        {"name": "Rust", "depth": "evidenced_commits"},
    ]
    assert len(row.work_history) == 1
    assert row.work_history[0]["company"] == "Google"
    assert row.work_history[0]["role_title"] == "Staff"
