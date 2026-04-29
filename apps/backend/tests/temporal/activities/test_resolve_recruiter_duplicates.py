"""Integration tests for `resolve_recruiter_duplicates` against real PG.

Mirrors the candidate-side `test_resolve_entity_duplicates.py` pattern: a real
session against the schema-creating engine + a `truncate_recruiters` fixture
to keep tests isolated.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.database import async_session_maker
from app.database.models import Recruiter
from app.schemas.product.recruiter import RecruiterProfile
from app.temporal.product.recruiter_indexing.activities.resolve_recruiter_duplicates import (
    resolve_recruiter_duplicates,
)


pytestmark = pytest.mark.usefixtures("truncate_recruiters")


def _profile_dict(**overrides) -> dict:
    base = {
        "recruiter_id": str(uuid.uuid4()),
        "full_name": "Sam Recruiter",
        "email": "sam@example.com",
    }
    base.update(overrides)
    return RecruiterProfile(**base).model_dump(mode="json")


async def _insert_recruiter(**fields) -> Recruiter:
    fields.setdefault("full_name", "Sam Recruiter")
    fields.setdefault("email", "sam@example.com")
    fields.setdefault("status", "pending")
    fields.setdefault("total_placements", 0)
    recruiter = Recruiter(**fields)
    async with async_session_maker() as sess:
        sess.add(recruiter)
        await sess.commit()
        await sess.refresh(recruiter)
    return recruiter


# ---------------------------------------------------------------------------
# resolve_recruiter_duplicates: real PG
# ---------------------------------------------------------------------------


async def test_existing_recruiter_returns_match_by_email():
    """Wizard pre-creates the row → activity returns is_duplicate=True with the canonical id."""
    existing = await _insert_recruiter(email="sam@example.com")

    result = await resolve_recruiter_duplicates(
        _profile_dict(recruiter_id=str(existing.id), email="sam@example.com")
    )

    assert result["is_duplicate"] is True
    assert result["existing_recruiter_id"] == str(existing.id)
    assert result["match_source"] == "email"


async def test_existing_match_when_wizard_uuid_mismatches_email():
    """Email is the source of truth; mismatched wizard-supplied id still resolves to the
    canonical recruiter row (defensive log path covered by unit logging assertions elsewhere)."""
    existing = await _insert_recruiter(email="sam@example.com")
    bogus_id = str(uuid.uuid4())

    result = await resolve_recruiter_duplicates(
        _profile_dict(recruiter_id=bogus_id, email="sam@example.com")
    )

    assert result["is_duplicate"] is True
    assert result["existing_recruiter_id"] == str(existing.id)
    assert result["match_source"] == "email"


async def test_missing_row_raises_value_error():
    """Per Decision 4: wizard MUST pre-create the recruiter; missing row is fail-fast."""
    with pytest.raises(ValueError, match="Recruiter row not found for email"):
        await resolve_recruiter_duplicates(
            _profile_dict(email="ghost@example.com")
        )
