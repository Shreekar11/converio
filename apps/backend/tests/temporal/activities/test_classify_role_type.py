"""Unit tests for `classify_role_type` (C1).

The LLM client is mocked via `patch(get_llm_client)`; no network or DB I/O.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.enums import CompanyStage, RemoteOnsite, RoleCategory, Seniority
from app.schemas.product.job import RoleClassification
from app.temporal.product.job_intake.activities.classify_role_type import (
    classify_role_type,
)

_ACTIVITY_MODULE = (
    "app.temporal.product.job_intake.activities.classify_role_type"
)


def _classification(**overrides) -> RoleClassification:
    base = dict(
        role_category=RoleCategory.ENGINEERING,
        seniority_level=Seniority.SENIOR,
        stage_fit=CompanyStage.SERIES_A,
        remote_onsite=RemoteOnsite.REMOTE,
        must_have_skills=["python", "distributed_systems"],
        nice_to_have_skills=["rust"],
        rationale="Senior backend engineer for a distributed-systems-heavy startup.",
    )
    base.update(overrides)
    return RoleClassification(**base)


def _payload(**overrides) -> dict:
    base = {
        "title": "Founding Engineer",
        "jd_text": "Build the platform.",
        "intake_notes": "Small team, generalist preferred.",
    }
    base.update(overrides)
    return base


async def test_happy_path_returns_classification_dict() -> None:
    """Mocked LLM yields a valid classification → activity returns its JSON dump."""
    fixture = _classification()
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(return_value=fixture)

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await classify_role_type(_payload())

    assert result["role_category"] == "engineering"
    assert result["seniority_level"] == "senior"
    assert result["stage_fit"] == "series_a"
    assert result["remote_onsite"] == "remote"
    assert result["must_have_skills"] == ["distributed_systems", "python"]
    assert result["nice_to_have_skills"] == ["rust"]
    assert "Senior" in result["rationale"]
    mock_llm.structured_complete.assert_awaited_once()


async def test_skill_normalization_round_trip() -> None:
    """Mixed-case / duplicate skills survive as sorted + lowercased + deduped.

    The Pydantic field validator runs on `RoleClassification` construction; this
    test asserts the activity does not undo that normalization on its way out.
    """
    fixture = RoleClassification(
        role_category=RoleCategory.ENGINEERING,
        seniority_level=Seniority.MID,
        stage_fit=None,
        remote_onsite=None,
        must_have_skills=["Python", "PYTHON", "FastAPI", "fastapi"],
        nice_to_have_skills=["Go ", "rust", "Rust"],
        rationale="Mid-level backend role.",
    )
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(return_value=fixture)

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        result = await classify_role_type(_payload())

    # Sorted, lowercased, deduped.
    assert result["must_have_skills"] == ["fastapi", "python"]
    assert result["nice_to_have_skills"] == ["go", "rust"]


async def test_llm_exception_propagates() -> None:
    """LLM raising any exception must NOT be swallowed — Temporal retry policy
    on the workflow side handles the retry."""
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(side_effect=RuntimeError("upstream 503"))

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        with pytest.raises(RuntimeError, match="upstream 503"):
            await classify_role_type(_payload())


async def test_missing_title_raises_value_error() -> None:
    """Inline payload validation rejects empty title before any LLM call."""
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock()

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        with pytest.raises(ValueError, match="title"):
            await classify_role_type({"title": "", "jd_text": "x"})

    mock_llm.structured_complete.assert_not_awaited()


async def test_intake_notes_optional() -> None:
    """`intake_notes=None` is allowed; activity still calls LLM with delimited prompt."""
    fixture = _classification()
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(return_value=fixture)

    with patch(f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm):
        await classify_role_type(_payload(intake_notes=None))

    mock_llm.structured_complete.assert_awaited_once()
    kwargs = mock_llm.structured_complete.await_args.kwargs
    user_msg = kwargs["messages"][1].content
    assert "(none)" in user_msg
