"""Tests for C1 activity: parse_resume.

Both `parse_document` (docling) and `get_llm_client` are stubbed — no real
document parsing or LLM calls happen.
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.product.candidate import (
    CandidateProfile,
    Skill,
    WorkHistoryItem,
)
from app.temporal.product.candidate_indexing.activities.parse_resume import (
    parse_resume,
)

_ACTIVITY_MODULE = (
    "app.temporal.product.candidate_indexing.activities.parse_resume"
)

FIXTURE_MARKDOWN = """# Jane Doe
**Email:** jane@example.com | **Location:** San Francisco

## Skills
Python, FastAPI, PostgreSQL

## Work History
- **Senior Engineer** at Stripe (2020 - 2023)
"""


def _make_profile(*, with_resume_text: bool) -> CandidateProfile:
    return CandidateProfile(
        full_name="Jane Doe",
        email="jane@example.com",
        location="San Francisco",
        seniority="senior",
        skills=[
            Skill(name="Python"),
            Skill(name="FastAPI"),
            Skill(name="PostgreSQL"),
        ],
        work_history=[
            WorkHistoryItem(
                company="Stripe",
                role_title="Senior Engineer",
                start_date="2020",
                end_date="2023",
            )
        ],
        resume_text=FIXTURE_MARKDOWN if with_resume_text else None,
    )


async def test_parse_resume_pdf_success() -> None:
    raw_bytes_b64 = base64.b64encode(b"fake pdf content").decode()
    fixture_profile = _make_profile(with_resume_text=True)

    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(return_value=fixture_profile)

    with patch(
        f"{_ACTIVITY_MODULE}.parse_document",
        new=AsyncMock(return_value=FIXTURE_MARKDOWN),
    ), patch(
        f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm
    ):
        result = await parse_resume(raw_bytes_b64, "application/pdf")

    assert result["full_name"] == "Jane Doe"
    assert result["email"] == "jane@example.com"
    assert len(result["skills"]) == 3
    assert {s["name"] for s in result["skills"]} == {
        "Python",
        "FastAPI",
        "PostgreSQL",
    }
    assert all(s["depth"] == "claimed_only" for s in result["skills"])
    assert len(result["work_history"]) == 1
    assert result["work_history"][0]["company"] == "Stripe"
    assert result["resume_text"] == FIXTURE_MARKDOWN
    mock_llm.structured_complete.assert_awaited_once()


async def test_resume_text_backfilled_when_missing() -> None:
    """If LLM returns a profile without resume_text, the activity backfills
    from the docling Markdown."""
    raw_bytes_b64 = base64.b64encode(b"fake pdf content").decode()
    profile_without_text = _make_profile(with_resume_text=False)
    assert profile_without_text.resume_text is None  # sanity check fixture

    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(return_value=profile_without_text)

    with patch(
        f"{_ACTIVITY_MODULE}.parse_document",
        new=AsyncMock(return_value=FIXTURE_MARKDOWN),
    ), patch(
        f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm
    ):
        result = await parse_resume(raw_bytes_b64, "application/pdf")

    assert result["resume_text"] == FIXTURE_MARKDOWN


async def test_parse_resume_decodes_base64_correctly() -> None:
    """The activity must base64-decode the input before passing bytes to docling."""
    raw_bytes = b"\x25PDF-1.4 fake binary"
    raw_bytes_b64 = base64.b64encode(raw_bytes).decode()

    parse_doc_mock = AsyncMock(return_value=FIXTURE_MARKDOWN)
    mock_llm = MagicMock()
    mock_llm.structured_complete = AsyncMock(
        return_value=_make_profile(with_resume_text=True)
    )

    with patch(
        f"{_ACTIVITY_MODULE}.parse_document", new=parse_doc_mock
    ), patch(
        f"{_ACTIVITY_MODULE}.get_llm_client", return_value=mock_llm
    ):
        await parse_resume(raw_bytes_b64, "application/pdf")

    parse_doc_mock.assert_awaited_once()
    args, _ = parse_doc_mock.call_args
    assert args[0] == raw_bytes
    assert args[1] == "application/pdf"
