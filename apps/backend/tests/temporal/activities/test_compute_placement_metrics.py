"""Integration tests for `compute_placement_metrics` against real PG.

Asserts the deterministic metric derivation (counts, stage grouping, fill_rate
flow-through) without exercising any LLM or external API.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.database.models import Recruiter, RecruiterPlacement
from app.temporal.product.recruiter_indexing.activities.compute_placement_metrics import (
    compute_placement_metrics,
)


pytestmark = pytest.mark.usefixtures("truncate_recruiters")


async def _insert_recruiter(**fields) -> Recruiter:
    fields.setdefault("full_name", "Sam Recruiter")
    fields.setdefault("email", f"sam-{uuid.uuid4().hex[:8]}@example.com")
    fields.setdefault("status", "pending")
    fields.setdefault("total_placements", 0)
    recruiter = Recruiter(**fields)
    async with async_session_maker() as sess:
        sess.add(recruiter)
        await sess.commit()
        await sess.refresh(recruiter)
    return recruiter


async def _insert_placements(recruiter_id: uuid.UUID, stages: list[str | None]) -> None:
    async with async_session_maker() as sess:
        for stage in stages:
            sess.add(
                RecruiterPlacement(
                    recruiter_id=recruiter_id,
                    candidate_name="Test Cand",
                    company_name="Test Co",
                    company_stage=stage,
                    role_title="SWE",
                )
            )
        await sess.commit()


async def test_empty_placements_returns_zero_total_and_empty_grouping():
    recruiter = await _insert_recruiter()

    out = await compute_placement_metrics(str(recruiter.id))

    assert out["total_placements"] == 0
    assert out["placements_by_stage"] == {}
    # No fill_rate pre-set on the recruiter row.
    assert out["fill_rate_pct"] is None
    assert out["avg_days_to_close"] is None


async def test_multi_stage_placements_grouped_correctly():
    """3 series_a + 2 seed → {'series_a': 3, 'seed': 2}."""
    recruiter = await _insert_recruiter()
    await _insert_placements(
        recruiter.id,
        ["series_a", "series_a", "series_a", "seed", "seed"],
    )

    out = await compute_placement_metrics(str(recruiter.id))

    assert out["total_placements"] == 5
    assert out["placements_by_stage"] == {"series_a": 3, "seed": 2}


async def test_null_stage_placements_skipped_from_grouping_but_counted_in_total():
    recruiter = await _insert_recruiter()
    # 2 known stage + 2 null-stage rows
    await _insert_placements(recruiter.id, ["series_a", "series_a", None, None])

    out = await compute_placement_metrics(str(recruiter.id))

    assert out["total_placements"] == 4
    assert out["placements_by_stage"] == {"series_a": 2}


async def test_preset_fill_rate_pct_flows_through():
    recruiter = await _insert_recruiter(fill_rate_pct=Decimal("85.50"))
    await _insert_placements(recruiter.id, ["seed"])

    out = await compute_placement_metrics(str(recruiter.id))

    assert out["fill_rate_pct"] == pytest.approx(85.50, abs=1e-2)


async def test_null_fill_rate_pct_returned_as_none():
    recruiter = await _insert_recruiter(fill_rate_pct=None)
    await _insert_placements(recruiter.id, ["seed"])

    out = await compute_placement_metrics(str(recruiter.id))

    assert out["fill_rate_pct"] is None


async def test_missing_recruiter_raises():
    bogus = str(uuid.uuid4())

    with pytest.raises(ValueError, match="not found in PG"):
        await compute_placement_metrics(bogus)
