"""Integration tests for B2 `resolve_entity_duplicates` against real PG."""
from __future__ import annotations

import pytest

from app.core.database import async_session_maker
from app.database.models import Candidate
from app.schemas.product.candidate import CandidateProfile
from app.temporal.product.candidate_indexing.activities.resolve_entity_duplicates import (
    compute_dedup_hash,
    resolve_entity_duplicates,
)


pytestmark = pytest.mark.usefixtures("truncate_candidates")


def _profile_dict(**overrides) -> dict:
    base = {"full_name": "Alice Smith", "email": "alice@example.com"}
    base.update(overrides)
    return CandidateProfile(**base).model_dump(mode="json")


async def _insert_candidate(**fields) -> Candidate:
    fields.setdefault("full_name", "Alice Smith")
    fields.setdefault("email", "alice@example.com")
    fields.setdefault(
        "dedup_hash",
        compute_dedup_hash(fields["full_name"], fields.get("email")),
    )
    fields.setdefault("status", "indexed")
    fields.setdefault("completeness_score", 0)
    candidate = Candidate(**fields)
    async with async_session_maker() as sess:
        sess.add(candidate)
        await sess.commit()
        await sess.refresh(candidate)
    return candidate


# ---------------------------------------------------------------------------
# compute_dedup_hash unit tests (pure function, no DB)
# ---------------------------------------------------------------------------


def test_compute_dedup_hash_normalizes_case_and_whitespace():
    h1 = compute_dedup_hash("Alice Smith", "alice@example.com")
    h2 = compute_dedup_hash("  alice smith  ", "  ALICE@example.com ")
    h3 = compute_dedup_hash("ALICE SMITH", " alice@example.com")
    assert h1 == h2 == h3


def test_compute_dedup_hash_handles_none_email():
    h = compute_dedup_hash("Alice", None)
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex


def test_compute_dedup_hash_distinguishes_different_people():
    h1 = compute_dedup_hash("Alice Smith", "alice@example.com")
    h2 = compute_dedup_hash("Bob Smith", "alice@example.com")
    assert h1 != h2


# ---------------------------------------------------------------------------
# resolve_entity_duplicates: end-to-end against real PG
# ---------------------------------------------------------------------------


async def test_no_duplicate_returns_is_duplicate_false():
    result = await resolve_entity_duplicates(_profile_dict())

    assert result["is_duplicate"] is False
    assert result["existing_candidate_id"] is None
    assert result["match_source"] is None


async def test_duplicate_via_dedup_hash():
    existing = await _insert_candidate(full_name="Alice Smith", email="alice@example.com")

    result = await resolve_entity_duplicates(_profile_dict())

    assert result["is_duplicate"] is True
    assert result["existing_candidate_id"] == str(existing.id)
    assert result["match_source"] == "dedup_hash"


async def test_duplicate_dedup_hash_normalizes_input():
    """Whitespace/case in the input profile should still match the stored hash."""
    existing = await _insert_candidate(full_name="Alice Smith", email="alice@example.com")

    result = await resolve_entity_duplicates(
        _profile_dict(full_name="  ALICE SMITH ", email=" Alice@Example.COM ")
    )

    assert result["is_duplicate"] is True
    assert result["existing_candidate_id"] == str(existing.id)
    assert result["match_source"] == "dedup_hash"


async def test_duplicate_via_github_username_when_dedup_hash_misses():
    """Different name+email but same github_username -> github_username match."""
    existing = await _insert_candidate(
        full_name="Alice Smith",
        email="alice@example.com",
        github_username="alice-smith",
    )

    result = await resolve_entity_duplicates(
        _profile_dict(
            full_name="Alice S.",  # changes dedup hash
            email="alice.s@newdomain.com",
            github_username="alice-smith",
        )
    )

    assert result["is_duplicate"] is True
    assert result["existing_candidate_id"] == str(existing.id)
    assert result["match_source"] == "github_username"


async def test_no_github_username_no_match():
    """If profile has no github_username, github fallback path is skipped."""
    await _insert_candidate(
        full_name="Alice Smith",
        email="alice@example.com",
        github_username="alice-smith",
    )

    result = await resolve_entity_duplicates(
        _profile_dict(full_name="Different Person", email="diff@x.com")
    )

    assert result["is_duplicate"] is False
    assert result["match_source"] is None


async def test_dedup_hash_takes_priority_over_github_username():
    """When both match, dedup_hash wins (checked first)."""
    existing = await _insert_candidate(
        full_name="Alice Smith",
        email="alice@example.com",
        github_username="alice-smith",
    )

    result = await resolve_entity_duplicates(
        _profile_dict(github_username="alice-smith")
    )

    assert result["is_duplicate"] is True
    assert result["existing_candidate_id"] == str(existing.id)
    assert result["match_source"] == "dedup_hash"
