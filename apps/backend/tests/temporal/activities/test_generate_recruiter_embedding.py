"""Unit tests for `generate_recruiter_embedding` with `embed_text` mocked.

Mirrors `test_generate_embedding.py` for the candidate side: hermetic, no
sentence-transformers load, no external IO.
"""
from __future__ import annotations

import uuid

import pytest

from app.schemas.enums import (
    CompanyStage,
    RecruitedFundingStage,
    RoleCategory,
    WorkspaceType,
)
from app.schemas.product.recruiter import (
    RecruiterClientItem,
    RecruiterPlacementItem,
    RecruiterProfile,
)
from app.temporal.product.recruiter_indexing.activities.generate_recruiter_embedding import (
    _build_blob,
    generate_recruiter_embedding,
)


EMBED_PATH = (
    "app.temporal.product.recruiter_indexing.activities."
    "generate_recruiter_embedding.embed_text"
)


@pytest.fixture
def mock_embed(mocker):
    """Patch embed_text so tests don't load sentence-transformers."""

    async def _stub(_text: str) -> list[float]:
        return [0.1] * 384

    return mocker.patch(EMBED_PATH, side_effect=_stub)


def _profile(**overrides) -> RecruiterProfile:
    base = dict(
        recruiter_id=str(uuid.uuid4()),
        full_name="Pat Smith",
        email="pat@example.com",
        bio="Decade of placing engineers at Series A startups.",
        domain_expertise=[RoleCategory.ENGINEERING, RoleCategory.DATA],
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SERIES_A,
        past_clients=[
            RecruiterClientItem(
                client_company_name="Stripe",
                role_focus=["backend", "platform"],
            )
        ],
        past_placements=[
            RecruiterPlacementItem(
                candidate_name="Alice",
                company_name="Stripe",
                company_stage=CompanyStage.SERIES_A,
                role_title="Senior Engineer",
            )
        ],
    )
    base.update(overrides)
    return RecruiterProfile(**base)


def _profile_dict(**overrides) -> dict:
    return _profile(**overrides).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Activity wrapper tests (with mocked embed_text)
# ---------------------------------------------------------------------------


async def test_returns_embedding_key_with_correct_dimension(mock_embed):
    out = await generate_recruiter_embedding(_profile_dict())

    assert set(out.keys()) == {"embedding"}
    assert isinstance(out["embedding"], list)
    assert len(out["embedding"]) == 384
    mock_embed.assert_called_once()


async def test_sparse_profile_does_not_crash(mock_embed):
    """Bare-minimum profile (no bio, no clients, no placements) still produces a 384-dim vector."""
    out = await generate_recruiter_embedding(
        _profile_dict(
            bio=None,
            domain_expertise=[],
            workspace_type=None,
            recruited_funding_stage=None,
            past_clients=[],
            past_placements=[],
        )
    )

    assert len(out["embedding"]) == 384
    text_arg = mock_embed.call_args.args[0]
    # Only full_name should be present; no leading/trailing pipe.
    assert "Pat Smith" in text_arg
    assert not text_arg.startswith(" | ")
    assert not text_arg.endswith(" | ")


async def test_embedding_text_includes_domain_clients_placements(mock_embed):
    await generate_recruiter_embedding(_profile_dict())

    text_arg = mock_embed.call_args.args[0]
    # Domain expertise rendered as comma-joined values.
    assert "domain expertise: engineering, data" in text_arg
    # Clients rendered with role_focus CSV.
    assert "Stripe (backend, platform)" in text_arg
    # Placements rendered as "<role> at <company> (<stage>)".
    assert "Senior Engineer at Stripe (series_a)" in text_arg


async def test_deterministic_output_for_same_input(mock_embed):
    out1 = await generate_recruiter_embedding(_profile_dict())
    out2 = await generate_recruiter_embedding(_profile_dict())
    assert out1["embedding"] == out2["embedding"]


# ---------------------------------------------------------------------------
# _build_blob pure-function tests (no mocking required)
# ---------------------------------------------------------------------------


def test_build_blob_includes_full_name_and_bio():
    blob = _build_blob(_profile())
    assert "Pat Smith" in blob
    assert "Decade of placing engineers" in blob


def test_build_blob_includes_workspace_and_funding_stage():
    blob = _build_blob(_profile())
    assert "agency" in blob
    assert "series_a" in blob


def test_build_blob_skips_empty_optional_pieces():
    blob = _build_blob(
        _profile(
            bio=None,
            workspace_type=None,
            recruited_funding_stage=None,
            past_clients=[],
            past_placements=[],
        )
    )
    # No leading/trailing pipe and no double-pipe.
    assert not blob.startswith(" | ")
    assert not blob.endswith(" | ")
    assert " |  | " not in blob


def test_build_blob_renders_multiple_placements_with_separator():
    profile = _profile(
        past_placements=[
            RecruiterPlacementItem(
                candidate_name="A",
                company_name="Stripe",
                company_stage=CompanyStage.SERIES_A,
                role_title="SWE",
            ),
            RecruiterPlacementItem(
                candidate_name="B",
                company_name="Notion",
                company_stage=CompanyStage.SERIES_B,
                role_title="Staff",
            ),
        ]
    )
    blob = _build_blob(profile)
    assert "SWE at Stripe (series_a)" in blob
    assert "Staff at Notion (series_b)" in blob
    # Pieces joined with ' ; '
    assert " ; " in blob
