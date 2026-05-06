"""Tests for `rank_and_select_recruiters` activity.

Pure-Python deterministic ranker — no LLM, no DB, no Temporal infrastructure.
We invoke the async function directly with `pytest.mark.asyncio`-style tests
(the suite-wide pytest-asyncio mode runs them via the session event loop).
"""
from __future__ import annotations

from app.temporal.product.recruiter_assignment.activities.rank_and_select_recruiters import (
    _WEIGHT_CLOSE_TIME,
    _WEIGHT_DOMAIN,
    _WEIGHT_FILL_RATE,
    _WEIGHT_SENIORITY,
    _WEIGHT_STAGE,
    rank_and_select_recruiters,
)


def _make_score(
    recruiter_id: str,
    *,
    domain: int = 80,
    stage: int = 70,
    seniority: int = 60,
    fill_rate: int = 90,
    close_time: int = 75,
    score: int = 80,
    confidence: float = 0.9,
    rationale: str = "Test rationale",
) -> dict:
    """Build a RecruiterFitScore-shaped dict the activity will validate."""
    return {
        "recruiter_id": recruiter_id,
        "score": score,
        "confidence": confidence,
        "rationale": rationale,
        "sub_scores": {
            "domain": domain,
            "stage": stage,
            "seniority": seniority,
            "fill_rate": fill_rate,
            "close_time": close_time,
        },
    }


# ---------------------------------------------------------------------------
# Happy path + ordering
# ---------------------------------------------------------------------------


async def test_happy_path_returns_top_n() -> None:
    """5 scored recruiters, top_n=3 -> 3 entries returned, sorted descending."""
    scores = [
        _make_score("r1", domain=90, fill_rate=90),  # high
        _make_score("r2", domain=50, fill_rate=50),  # low
        _make_score("r3", domain=80, fill_rate=80),  # mid
        _make_score("r4", domain=10, fill_rate=10),  # lowest
        _make_score("r5", domain=70, fill_rate=70),  # mid-low
    ]

    result = await rank_and_select_recruiters({"scores": scores, "top_n": 3})

    assert result["top_n"] == 3
    assert result["total_scored"] == 5
    ranked = result["ranked"]
    assert len(ranked) == 3

    # Strictly descending by weighted_score.
    weighted_scores = [r["weighted_score"] for r in ranked]
    assert weighted_scores == sorted(weighted_scores, reverse=True)

    # Highest-domain entry should appear first given the dominant weight.
    assert ranked[0]["recruiter_id"] == "r1"


# ---------------------------------------------------------------------------
# Clamping of `top_n`
# ---------------------------------------------------------------------------


async def test_top_n_clamped_high() -> None:
    """top_n=99 clamps down to the supported maximum (10)."""
    scores = [_make_score(f"r{i}") for i in range(3)]
    result = await rank_and_select_recruiters({"scores": scores, "top_n": 99})
    assert result["top_n"] == 10
    # The actual returned list is bounded by the input size.
    assert len(result["ranked"]) == 3


async def test_top_n_clamped_low() -> None:
    """top_n=0 clamps up to the supported minimum (1)."""
    scores = [_make_score(f"r{i}") for i in range(3)]
    result = await rank_and_select_recruiters({"scores": scores, "top_n": 0})
    assert result["top_n"] == 1
    assert len(result["ranked"]) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_empty_input() -> None:
    """No scores -> empty `ranked` list, no exception."""
    result = await rank_and_select_recruiters({"scores": [], "top_n": 5})
    assert result["ranked"] == []
    assert result["total_scored"] == 0


# ---------------------------------------------------------------------------
# Weight invariants
# ---------------------------------------------------------------------------


def test_weights_sum_to_one() -> None:
    """The convex-combination invariant: weights must sum to exactly 1.0."""
    total = (
        _WEIGHT_DOMAIN
        + _WEIGHT_FILL_RATE
        + _WEIGHT_STAGE
        + _WEIGHT_CLOSE_TIME
        + _WEIGHT_SENIORITY
    )
    assert abs(total - 1.0) < 1e-9


async def test_weighted_score_formula() -> None:
    """An all-100 sub_scores recruiter must score exactly 100.0 weighted."""
    score = _make_score(
        "r-perfect",
        domain=100,
        stage=100,
        seniority=100,
        fill_rate=100,
        close_time=100,
        score=100,
    )
    result = await rank_and_select_recruiters({"scores": [score], "top_n": 1})
    assert len(result["ranked"]) == 1
    assert result["ranked"][0]["weighted_score"] == 100.0
    assert result["ranked"][0]["recruiter_id"] == "r-perfect"
