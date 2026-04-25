"""Unit tests for B1 `generate_embedding` with `embed_text` mocked."""
from __future__ import annotations

import pytest

from app.schemas.product.candidate import CandidateProfile, Skill, WorkHistoryItem
from app.temporal.product.candidate_indexing.activities.generate_embedding import (
    generate_embedding,
)

EMBED_PATH = (
    "app.temporal.product.candidate_indexing.activities.generate_embedding.embed_text"
)


@pytest.fixture
def mock_embed(mocker):
    """Patch embed_text so tests don't load sentence-transformers."""
    return mocker.patch(EMBED_PATH, return_value=[0.1] * 384)


def _profile_dict(**overrides) -> dict:
    base = dict(full_name="Alice Smith", seniority="senior", location="SF")
    base.update(overrides)
    return CandidateProfile(**base).model_dump(mode="json")


async def test_returns_embedding_key_with_correct_dimension(mock_embed):
    out = await generate_embedding(_profile_dict())

    assert set(out.keys()) == {"embedding"}
    assert isinstance(out["embedding"], list)
    assert len(out["embedding"]) == 384
    mock_embed.assert_called_once()


async def test_embedding_text_includes_name_seniority_location(mock_embed):
    await generate_embedding(_profile_dict(full_name="Alice", seniority="senior", location="SF"))

    text_arg = mock_embed.call_args.args[0]
    assert "Alice" in text_arg
    assert "senior" in text_arg
    assert "SF" in text_arg


async def test_embedding_text_includes_skills_csv(mock_embed):
    profile = _profile_dict(
        skills=[Skill(name="Python"), Skill(name="FastAPI"), Skill(name="PostgreSQL")]
    )

    await generate_embedding(profile)

    text_arg = mock_embed.call_args.args[0]
    assert "Python, FastAPI, PostgreSQL" in text_arg


async def test_embedding_text_includes_work_history(mock_embed):
    profile = _profile_dict(
        work_history=[
            WorkHistoryItem(company="Stripe", role_title="SWE"),
            WorkHistoryItem(company="Google", role_title="Staff"),
        ]
    )

    await generate_embedding(profile)

    text_arg = mock_embed.call_args.args[0]
    assert "SWE at Stripe" in text_arg
    assert "Staff at Google" in text_arg


async def test_handles_empty_optional_fields(mock_embed):
    """Missing seniority/location/skills/work_history should not crash."""
    out = await generate_embedding(_profile_dict(seniority=None, location=None))

    assert len(out["embedding"]) == 384
    text_arg = mock_embed.call_args.args[0]
    # Empty pieces are filtered, so no leading/trailing separator issues
    assert "Alice Smith" in text_arg
    assert not text_arg.startswith(" | ")
    assert not text_arg.endswith(" | ")


async def test_deterministic_output_for_same_input(mock_embed):
    out1 = await generate_embedding(_profile_dict())
    out2 = await generate_embedding(_profile_dict())
    assert out1["embedding"] == out2["embedding"]
