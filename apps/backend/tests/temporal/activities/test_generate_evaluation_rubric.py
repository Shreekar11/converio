"""Unit tests for `generate_evaluation_rubric` (C2).

LLM client mocked via `patch(get_llm_client)`. The activity uses `.complete`
(raw JSON) so we can normalize names + weights before model construction.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.llm.base import LLMResponse
from app.temporal.product.job_intake.activities.generate_evaluation_rubric import (
    generate_evaluation_rubric,
)

_ACTIVITY_MODULE = (
    "app.temporal.product.job_intake.activities.generate_evaluation_rubric"
)
_ACTIVITY_LOGGER = "app.temporal.product.job_intake.activities.generate_evaluation_rubric"


def _llm_response(payload: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload),
        model="test-model",
        provider="fake",
        raw=None,
    )


def _classification() -> dict:
    return {
        "role_category": "engineering",
        "seniority_level": "senior",
        "stage_fit": "series_a",
        "remote_onsite": "remote",
        "must_have_skills": ["python", "distributed_systems"],
        "nice_to_have_skills": ["rust"],
        "rationale": "Senior backend engineer.",
    }


def _payload(**overrides) -> dict:
    base = {
        "classification": _classification(),
        "intake_notes": "Small team; generalist preferred.",
        "extra": None,
    }
    base.update(overrides)
    return base


def _dim(name: str, weight: float, **overrides) -> dict:
    base = {
        "name": name,
        "description": f"description for {name}.",
        "weight": weight,
        "evaluation_guidance": f"Score 0-5 based on {name}.",
    }
    base.update(overrides)
    return base


async def test_happy_path_six_dimensions_summing_to_one() -> None:
    """Six well-formed dimensions whose weights sum to 1.0 → returned sorted by (-weight, name)."""
    payload = {
        "rationale": "Weights calibrated to a founding-engineer profile.",
        "dimensions": [
            _dim("distributed_systems_depth", 0.25),
            _dim("full_stack_ownership", 0.20),
            _dim("startup_stage_fit", 0.20),
            _dim("open_source_signals", 0.15),
            _dim("communication_clarity", 0.10),
            _dim("system_design_thinking", 0.10),
        ],
    }
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=_llm_response(payload))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await generate_evaluation_rubric(_payload())

    assert len(result["dimensions"]) == 6
    weights = [d["weight"] for d in result["dimensions"]]
    # Deterministic sort: weights monotone non-increasing.
    assert weights == sorted(weights, reverse=True)
    # First dim is the heaviest.
    assert result["dimensions"][0]["name"] == "distributed_systems_depth"
    # Sum normalized to 1.0 (round-trip floats).
    assert sum(weights) == pytest.approx(1.0, abs=1e-3)


async def test_weight_drift_renormalized_with_warning(caplog) -> None:
    """Weights summing to 0.9 → renormalized to 1.0; drift > 0.05 logs a warning."""
    payload = {
        "rationale": "Drift case.",
        "dimensions": [
            _dim("dim_a", 0.30),
            _dim("dim_b", 0.20),
            _dim("dim_c", 0.20),
            _dim("dim_d", 0.20),
        ],  # sums to 0.90 — drift 0.10
    }
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=_llm_response(payload))

    with caplog.at_level(logging.WARNING, logger=_ACTIVITY_LOGGER):
        with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
            result = await generate_evaluation_rubric(_payload())

    weights = [d["weight"] for d in result["dimensions"]]
    assert sum(weights) == pytest.approx(1.0, abs=1e-3)
    # Warning emitted about drift.
    assert any(
        "drift" in record.getMessage().lower() for record in caplog.records
    ), f"expected drift warning in: {[r.getMessage() for r in caplog.records]}"


async def test_name_normalization_to_snake_case() -> None:
    """Free-text names like 'Distributed Systems Depth' → 'distributed_systems_depth'.

    `RubricDimension.name` enforces a strict snake_case regex; the activity must
    normalize BEFORE constructing the model or Pydantic would reject the output.
    """
    payload = {
        "rationale": "Mixed-case names.",
        "dimensions": [
            _dim("Distributed Systems Depth", 0.30),
            _dim("Full-Stack Ownership", 0.25),
            _dim("OSS / Open Source", 0.20),
            _dim("Communication", 0.25),
        ],
    }
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=_llm_response(payload))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await generate_evaluation_rubric(_payload())

    names = {d["name"] for d in result["dimensions"]}
    assert "distributed_systems_depth" in names
    assert "full_stack_ownership" in names
    assert "oss_open_source" in names
    assert "communication" in names


async def test_too_few_dimensions_raises() -> None:
    """Three dimensions → ValueError (minimum is 4)."""
    payload = {
        "rationale": "Too few.",
        "dimensions": [
            _dim("alpha", 0.4),
            _dim("beta", 0.3),
            _dim("gamma", 0.3),
        ],
    }
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=_llm_response(payload))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        with pytest.raises(ValueError, match="at least 4 dimensions"):
            await generate_evaluation_rubric(_payload())


async def test_too_many_dimensions_truncated_to_top_eight_by_weight() -> None:
    """Ten dimensions → truncated to the top 8 by weight."""
    payload = {
        "rationale": "Excess dimensions.",
        "dimensions": [
            _dim(f"dim_{i:02d}", weight)
            for i, weight in enumerate(
                [0.20, 0.15, 0.12, 0.10, 0.10, 0.08, 0.08, 0.07, 0.05, 0.05]
            )
        ],
    }
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=_llm_response(payload))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await generate_evaluation_rubric(_payload())

    assert len(result["dimensions"]) == 8
    kept = {d["name"] for d in result["dimensions"]}
    # The two lowest-weight dims (0.05, 0.05 for dim_08 and dim_09) get dropped.
    assert "dim_00" in kept  # heaviest survives
    assert "dim_08" not in kept and "dim_09" not in kept
    weights = [d["weight"] for d in result["dimensions"]]
    assert sum(weights) == pytest.approx(1.0, abs=1e-3)


async def test_llm_exception_propagates() -> None:
    """Activity does NOT swallow LLM errors; Temporal retry handles them."""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("upstream 503"))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        with pytest.raises(RuntimeError, match="upstream 503"):
            await generate_evaluation_rubric(_payload())


async def test_code_fenced_json_is_parsed() -> None:
    """LLM wrapping output in ```json ... ``` is tolerated."""
    inner = {
        "rationale": "Fenced.",
        "dimensions": [
            _dim("alpha", 0.25),
            _dim("beta", 0.25),
            _dim("gamma", 0.25),
            _dim("delta", 0.25),
        ],
    }
    fenced = "```json\n" + json.dumps(inner) + "\n```"
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=LLMResponse(content=fenced, model="test", provider="fake")
    )

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await generate_evaluation_rubric(_payload())

    assert len(result["dimensions"]) == 4


async def test_invalid_classification_raises() -> None:
    """Missing classification → ValueError before any LLM call."""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock()

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        with pytest.raises(ValueError, match="classification"):
            await generate_evaluation_rubric({"classification": None})

    mock_llm.complete.assert_not_awaited()
