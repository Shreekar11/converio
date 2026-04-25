"""Unit tests for the pure `_compute_score` function in B4."""
from __future__ import annotations

import pytest

from app.schemas.product.candidate import (
    CandidateProfile,
    EducationItem,
    GitHubSignals,
    Skill,
    WorkHistoryItem,
)
from app.temporal.product.candidate_indexing.activities.score_profile_completeness import (
    _WEIGHTS,
    _compute_score,
)


def _make_profile(**kwargs) -> CandidateProfile:
    kwargs.setdefault("full_name", "Test Person")
    return CandidateProfile(**kwargs)


def test_weights_sum_to_one():
    """Sanity: completeness weights are a probability distribution."""
    assert round(sum(_WEIGHTS.values()), 5) == 1.0


def test_full_profile_with_github_signals_scores_above_threshold():
    profile = _make_profile(
        full_name="Alice Smith",
        email="alice@example.com",
        seniority="senior",
        years_experience=8,
        skills=[Skill(name="Python"), Skill(name="FastAPI"), Skill(name="PostgreSQL")],
        work_history=[WorkHistoryItem(company="Stripe", role_title="SWE")],
        education=[EducationItem(institution="MIT")],
        github_username="alice-smith",
        resume_text="Experienced engineer.",
        location="San Francisco",
        stage_fit=["series_a", "series_b"],
    )
    github = GitHubSignals(repo_count=20, commits_12m=300, stars_total=50, top_language="Python")

    score = _compute_score(profile, github)

    assert score >= 0.9
    assert score == pytest.approx(1.0, abs=0.01)


def test_only_name_present_yields_minimal_score_and_review_required():
    profile = _make_profile(full_name="Bob Jones")
    github = GitHubSignals()

    score = _compute_score(profile, github)

    assert score == _WEIGHTS["name"]
    assert score == pytest.approx(0.05, abs=1e-9)
    assert score < 0.5  # would trigger review_queue


def test_mid_profile_without_github_passes_threshold():
    """Name + email + seniority + 3 skills + 1 work_history -> >= 0.5."""
    profile = _make_profile(
        full_name="Carol",
        email="carol@x.com",
        seniority="mid",
        skills=[Skill(name="Go"), Skill(name="gRPC"), Skill(name="Redis")],
        work_history=[WorkHistoryItem(company="Acme", role_title="Dev")],
    )
    github = GitHubSignals()

    score = _compute_score(profile, github)
    expected = (
        _WEIGHTS["name"]
        + _WEIGHTS["email"]
        + _WEIGHTS["seniority"]
        + _WEIGHTS["skills_3plus"]
        + _WEIGHTS["work_history_1plus"]
    )
    assert score == pytest.approx(expected, abs=0.01)
    assert score >= 0.5  # not review_queue


def test_github_username_without_signals_does_not_count():
    """github_username alone (empty signals) must NOT add the github weight."""
    profile = _make_profile(full_name="Dan", github_username="dan")
    github = GitHubSignals()  # repo_count=0 -> is_empty()

    score = _compute_score(profile, github)

    assert score == _WEIGHTS["name"]


def test_github_username_with_signals_adds_github_weight():
    profile = _make_profile(full_name="Eve", github_username="eve")
    github = GitHubSignals(repo_count=5)

    score = _compute_score(profile, github)

    assert score == pytest.approx(_WEIGHTS["name"] + _WEIGHTS["github"], abs=0.01)


def test_two_skills_does_not_count_skills_3plus():
    profile = _make_profile(
        full_name="Frank",
        skills=[Skill(name="Rust"), Skill(name="Tokio")],
    )
    score = _compute_score(profile, GitHubSignals())
    assert score == _WEIGHTS["name"]  # skills_3plus weight NOT added


def test_score_is_rounded_to_two_decimals():
    profile = _make_profile(full_name="Grace", email="g@x.com", seniority="staff")
    score = _compute_score(profile, GitHubSignals())
    # Verify .2 decimal rounding behavior
    assert score == round(score, 2)


@pytest.mark.parametrize(
    "field_name,profile_kwargs,github_signals,expected_extra_weight",
    [
        ("email", {"email": "x@y.com"}, GitHubSignals(), _WEIGHTS["email"]),
        ("seniority", {"seniority": "senior"}, GitHubSignals(), _WEIGHTS["seniority"]),
        ("years_experience", {"years_experience": 5}, GitHubSignals(), _WEIGHTS["years_experience"]),
        ("education", {"education": [EducationItem(institution="MIT")]}, GitHubSignals(), _WEIGHTS["education"]),
        ("resume_text", {"resume_text": "blah"}, GitHubSignals(), _WEIGHTS["resume_text"]),
        ("location", {"location": "SF"}, GitHubSignals(), _WEIGHTS["location"]),
        ("stage_fit", {"stage_fit": ["seed"]}, GitHubSignals(), _WEIGHTS["stage_fit"]),
        (
            "work_history_1plus",
            {"work_history": [WorkHistoryItem(company="A", role_title="B")]},
            GitHubSignals(),
            _WEIGHTS["work_history_1plus"],
        ),
        (
            "skills_3plus",
            {"skills": [Skill(name="a"), Skill(name="b"), Skill(name="c")]},
            GitHubSignals(),
            _WEIGHTS["skills_3plus"],
        ),
    ],
)
def test_each_field_contributes_its_weight(
    field_name, profile_kwargs, github_signals, expected_extra_weight
):
    """Each individual field, when added on top of name, contributes its own weight."""
    base = _compute_score(_make_profile(full_name="Test"), GitHubSignals())
    profile = _make_profile(full_name="Test", **profile_kwargs)
    score = _compute_score(profile, github_signals)
    assert score == pytest.approx(base + expected_extra_weight, abs=0.01), (
        f"field={field_name} expected={base + expected_extra_weight} got={score}"
    )
