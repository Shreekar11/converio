"""Unit + integration tests for `score_recruiter_credibility`.

Pure-function `_compute_score` is unit-tested with no DB. The activity wrapper
is exercised against real PG via `truncate_recruiters` to assert the status +
extra writeback path. Mirrors `test_score_profile_completeness.py` style.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.database.models import Recruiter
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
from app.temporal.product.recruiter_indexing.activities.score_recruiter_credibility import (
    _WEIGHTS,
    _compute_score,
    score_recruiter_credibility,
)


# ---------------------------------------------------------------------------
# Pure-function _compute_score: hermetic, no DB
# ---------------------------------------------------------------------------


def _profile(**overrides) -> RecruiterProfile:
    base = dict(
        recruiter_id=str(uuid.uuid4()),
        full_name="Pat Smith",
        email="pat@example.com",
    )
    base.update(overrides)
    return RecruiterProfile(**base)


def _placements(stages: list[CompanyStage | None]) -> list[RecruiterPlacementItem]:
    return [
        RecruiterPlacementItem(
            candidate_name=f"C{i}",
            company_name=f"Co{i}",
            company_stage=stage,
            role_title="SWE",
        )
        for i, stage in enumerate(stages)
    ]


def test_weights_sum_to_one():
    """Sanity: credibility weights are a probability distribution."""
    assert round(sum(_WEIGHTS.values()), 5) == 1.0


def test_full_credibility_all_signals_present():
    profile = _profile(
        bio="Decade in placements",
        linkedin_url="https://linkedin.com/in/pat",
        domain_expertise=[RoleCategory.ENGINEERING],
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SERIES_A,
        past_clients=[
            RecruiterClientItem(client_company_name="Stripe", role_focus=["backend"])
        ],
        past_placements=_placements(
            [CompanyStage.SERIES_A, CompanyStage.SEED, CompanyStage.SERIES_B]
        ),
    )

    score = _compute_score(profile)

    assert score == pytest.approx(1.0, abs=1e-9)


def test_minimal_profile_only_full_name_and_email_low_score():
    """Just full_name + email → 0.0 (no scored signal). status='pending' once routed."""
    profile = _profile()
    assert _compute_score(profile) == pytest.approx(0.0, abs=1e-9)


def test_threshold_just_below_0_50_routes_to_pending():
    """Boundary: a score just under 0.5 must trigger `status='pending'`.

    Weight set is discrete (steps of 0.05), so 0.49 is unreachable; the closest
    sub-threshold value is 0.45. Mirrors plan §Phase 5 Task 5.1 ("threshold cases
    at 0.49"): we exercise the highest achievable sub-threshold combination.
    """
    # 0.15 (bio) + 0.10 (linkedin) + 0.10 (workspace) + 0.10 (funding) = 0.45.
    # 2 placements in a single stage → no stages_2plus, no placements_3plus.
    profile = _profile(
        bio="bio text",
        linkedin_url="https://linkedin.com/in/x",
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SEED,
        past_placements=_placements([CompanyStage.SEED, CompanyStage.SEED]),
    )
    score = _compute_score(profile)
    assert score == pytest.approx(0.45, abs=1e-9)
    assert score < 0.5  # routes to pending


def test_threshold_at_0_50_routes_to_active():
    """Boundary: score == 0.50 must NOT trigger review (status='active')."""
    # 0.15 + 0.10 + 0.10 + 0.10 + 0.05 (stages_2plus) = 0.50
    profile = _profile(
        bio="bio text",
        linkedin_url="https://linkedin.com/in/x",
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SEED,
        past_placements=_placements([CompanyStage.SEED, CompanyStage.SERIES_A]),
    )
    score = _compute_score(profile)
    assert score == pytest.approx(0.50, abs=1e-9)
    assert score >= 0.5  # routes to active


def test_placements_span_2_stages_awards_stages_2plus_weight():
    profile = _profile(
        past_placements=_placements([CompanyStage.SEED, CompanyStage.SERIES_A]),
    )
    # No other signals → only stages_2plus credit.
    assert _compute_score(profile) == pytest.approx(_WEIGHTS["stages_2plus"], abs=1e-9)


def test_placements_all_single_stage_no_stages_2plus_credit():
    profile = _profile(
        past_placements=_placements([CompanyStage.SEED, CompanyStage.SEED, CompanyStage.SEED]),
    )
    # 3 placements in a single stage → only placements_3plus weight, NOT stages_2plus.
    assert _compute_score(profile) == pytest.approx(_WEIGHTS["placements_3plus"], abs=1e-9)


def test_score_rounded_to_two_decimals():
    profile = _profile(bio="x")
    score = _compute_score(profile)
    assert score == round(score, 2)


# ---------------------------------------------------------------------------
# Activity wrapper: writes status + extra to PG (real PG via truncate_recruiters)
# ---------------------------------------------------------------------------


async def _insert_recruiter(**fields) -> Recruiter:
    fields.setdefault("full_name", "Pat Smith")
    fields.setdefault("email", f"pat-{uuid.uuid4().hex[:8]}@example.com")
    fields.setdefault("status", "pending")
    fields.setdefault("total_placements", 0)
    recruiter = Recruiter(**fields)
    async with async_session_maker() as sess:
        sess.add(recruiter)
        await sess.commit()
        await sess.refresh(recruiter)
    return recruiter


@pytest.mark.usefixtures("truncate_recruiters")
async def test_activity_persists_status_active_for_full_credibility():
    recruiter = await _insert_recruiter()
    profile_dict = _profile(
        bio="Decade",
        linkedin_url="https://linkedin.com/in/pat",
        domain_expertise=[RoleCategory.ENGINEERING],
        workspace_type=WorkspaceType.AGENCY,
        recruited_funding_stage=RecruitedFundingStage.SERIES_A,
        past_clients=[
            RecruiterClientItem(client_company_name="Stripe", role_focus=["backend"])
        ],
        past_placements=_placements(
            [CompanyStage.SERIES_A, CompanyStage.SEED, CompanyStage.SERIES_B]
        ),
    ).model_dump(mode="json")

    out = await score_recruiter_credibility(str(recruiter.id), profile_dict)

    assert out["status"] == "active"
    assert out["review_required"] is False
    assert out["credibility_score"] == pytest.approx(1.0, abs=1e-9)

    async with async_session_maker() as sess:
        row = (
            await sess.execute(select(Recruiter).where(Recruiter.id == recruiter.id))
        ).scalar_one()
    assert row.status == "active"
    assert row.extra is not None
    assert row.extra["credibility_score"] == pytest.approx(1.0, abs=1e-9)


@pytest.mark.usefixtures("truncate_recruiters")
async def test_activity_persists_status_pending_for_minimal_profile():
    recruiter = await _insert_recruiter()
    profile_dict = _profile().model_dump(mode="json")

    out = await score_recruiter_credibility(str(recruiter.id), profile_dict)

    assert out["status"] == "pending"
    assert out["review_required"] is True
    assert out["credibility_score"] < 0.5


@pytest.mark.usefixtures("truncate_recruiters")
async def test_activity_missing_recruiter_logs_warning_does_not_raise():
    """Per activity contract: missing PG row is a warning + return — graph already written."""
    bogus = str(uuid.uuid4())
    profile_dict = _profile().model_dump(mode="json")

    out = await score_recruiter_credibility(bogus, profile_dict)

    assert out["status"] in {"pending", "active"}
    assert "credibility_score" in out
