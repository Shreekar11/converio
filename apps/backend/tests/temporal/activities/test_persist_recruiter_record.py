"""Integration tests for `persist_recruiter_record` against real PG.

Asserts upsert semantics: fresh metrics + embedding are written, but pre-set
recruiter values (e.g. fill_rate_pct that derivation can't supply) are
preserved across runs. Mirrors `test_persist_candidate_record.py` style.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.database.models import Recruiter
from app.temporal.product.recruiter_indexing.activities.persist_recruiter_record import (
    persist_recruiter_record,
)


pytestmark = pytest.mark.usefixtures("truncate_recruiters")


def _embedding(value: float = 0.1) -> list[float]:
    return [value] * 384


def _metrics(**overrides) -> dict:
    base = dict(
        fill_rate_pct=72.5,
        avg_days_to_close=21,
        total_placements=4,
        placements_by_stage={"series_a": 3, "seed": 1},
    )
    base.update(overrides)
    return base


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


async def _fetch(recruiter_id: str) -> Recruiter | None:
    async with async_session_maker() as sess:
        result = await sess.execute(
            select(Recruiter).where(Recruiter.id == uuid.UUID(recruiter_id))
        )
        return result.scalar_one_or_none()


async def test_happy_path_writes_embedding_metrics_and_extra():
    recruiter = await _insert_recruiter()

    out = await persist_recruiter_record(
        recruiter_id=str(recruiter.id),
        embedding=_embedding(0.2),
        metrics_data=_metrics(),
    )

    assert out["recruiter_id"] == str(recruiter.id)

    row = await _fetch(str(recruiter.id))
    assert row is not None
    assert row.total_placements == 4
    assert float(row.fill_rate_pct) == pytest.approx(72.5, abs=1e-2)
    assert row.avg_days_to_close == 21
    assert row.embedding is not None
    stored = list(row.embedding)
    assert len(stored) == 384
    # extra now carries placements_by_stage snapshot for downstream Agent 0.
    assert row.extra is not None
    assert row.extra["placements_by_stage"] == {"series_a": 3, "seed": 1}


async def test_null_fill_rate_preserves_existing_value():
    """Metrics derivation without fill_rate must not clobber a pre-set DB value."""
    recruiter = await _insert_recruiter(fill_rate_pct=Decimal("90.00"))

    await persist_recruiter_record(
        recruiter_id=str(recruiter.id),
        embedding=_embedding(),
        metrics_data=_metrics(fill_rate_pct=None, avg_days_to_close=None),
    )

    row = await _fetch(str(recruiter.id))
    assert row is not None
    assert float(row.fill_rate_pct) == pytest.approx(90.00, abs=1e-2)


async def test_existing_extra_keys_preserved_alongside_new_placements_by_stage():
    """`extra` is a JSONB merge, not a replace — pre-existing keys must survive."""
    recruiter = await _insert_recruiter(extra={"unrelated_key": "preserve_me"})

    await persist_recruiter_record(
        recruiter_id=str(recruiter.id),
        embedding=_embedding(),
        metrics_data=_metrics(placements_by_stage={"seed": 2}),
    )

    row = await _fetch(str(recruiter.id))
    assert row is not None
    assert row.extra is not None
    assert row.extra["unrelated_key"] == "preserve_me"
    assert row.extra["placements_by_stage"] == {"seed": 2}


async def test_recruiter_not_found_raises():
    bogus = str(uuid.uuid4())

    with pytest.raises(ValueError, match="not found in PG"):
        await persist_recruiter_record(
            recruiter_id=bogus,
            embedding=_embedding(),
            metrics_data=_metrics(),
        )


async def test_explicit_fill_rate_pct_updated():
    recruiter = await _insert_recruiter(fill_rate_pct=Decimal("50.00"))

    await persist_recruiter_record(
        recruiter_id=str(recruiter.id),
        embedding=_embedding(),
        metrics_data=_metrics(fill_rate_pct=88.00),
    )

    row = await _fetch(str(recruiter.id))
    assert row is not None
    assert float(row.fill_rate_pct) == pytest.approx(88.00, abs=1e-2)
