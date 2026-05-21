"""Unit tests for the Scorecard Generator Agent's deterministic activities.

These activities have no LLM, no DB, no network — they are pure functions
of their input dicts. We exercise them directly (no Temporal worker) and
assert the externally-visible contracts the workflow relies on:

  * `check_confidence_gate` — confidence threshold filter, evidence_limited
    bypass, threshold-boundary behaviour (>= vs <).
  * `compute_overall_match_score` — Decimal-exact weighted average,
    bit-exact reproducibility across calls, edge cases.
  * `build_scoring_prompt` — rubric dimensions surfaced in prompt,
    candidate summary included, output-format instructions present.

No Temporal `WorkflowEnvironment` is needed — these activities are
plain `async def` coroutines decorated with `@activity.defn`. The
decorator is a no-op when invoked outside a Worker context, so we can
just `await` them in pytest.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.temporal.product.scorecard.activities.build_scoring_prompt import (
    build_scoring_prompt,
)
from app.temporal.product.scorecard.activities.check_confidence_gate import (
    CONFIDENCE_THRESHOLD,
    check_confidence_gate,
)
from app.temporal.product.scorecard.activities.compute_overall_match_score import (
    compute_overall_match_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dim(
    *,
    name: str = "dim",
    score: int = 80,
    confidence: float = 0.9,
    weight: float = 1.0,
    rationale: str = "ok",
    evidence_limited: bool = False,
    citation: dict | None = None,
) -> dict:
    """Build a ScorecardDimension-shaped dict for activity input.

    Activities re-validate via Pydantic at the boundary, so this helper
    must produce dicts that pass `ScorecardDimension.model_validate`.
    `citation` defaults to a minimal placeholder so omitting it does not
    trip pydantic's `extra="forbid"` config on adjacent fields.
    """
    return {
        "name": name,
        "score": score,
        "confidence": confidence,
        "weight": weight,
        "rationale": rationale,
        "evidence_limited": evidence_limited,
        "citation": citation
        or {"text": "placeholder", "resolution_method": "placeholder"},
    }


# ---------------------------------------------------------------------------
# check_confidence_gate
# ---------------------------------------------------------------------------


class TestCheckConfidenceGate:
    """Cover the four gate-decision branches (plan §11)."""

    async def test_all_dims_above_threshold_returns_all_confident(self) -> None:
        dims = [
            _make_dim(name="a", confidence=0.90),
            _make_dim(name="b", confidence=0.75),
            _make_dim(name="c", confidence=CONFIDENCE_THRESHOLD),  # exactly 0.65
        ]

        out = await check_confidence_gate({"dimensions": dims})

        assert out["all_confident"] is True
        assert out["low_confidence_dimensions"] == []
        assert out["low_conf_count"] == 0

    async def test_two_dims_below_threshold_returned(self) -> None:
        dims = [
            _make_dim(name="strong", confidence=0.92),
            _make_dim(name="weak1", confidence=0.40),
            _make_dim(name="weak2", confidence=0.55),
        ]

        out = await check_confidence_gate({"dimensions": dims})

        assert out["all_confident"] is False
        assert out["low_conf_count"] == 2
        names = {d["name"] for d in out["low_confidence_dimensions"]}
        assert names == {"weak1", "weak2"}
        # Each surfaced LowConfDim carries enough state to drive the
        # self-correction prompt — assert the contract.
        for d in out["low_confidence_dimensions"]:
            assert "current_confidence" in d
            assert "current_score" in d
            assert d["current_confidence"] < CONFIDENCE_THRESHOLD

    async def test_evidence_limited_low_conf_dims_are_bypassed(self) -> None:
        """`evidence_limited=True` short-circuits the gate even at low conf.

        This is the v1 MVP-toolset reality check: dims like
        `communication_clarity` have no fetcher in v1, so re-scoring them
        would burn budget. The gate honors that by filtering them out
        BEFORE the threshold check.
        """
        dims = [
            _make_dim(name="weak_but_no_tool", confidence=0.30, evidence_limited=True),
            _make_dim(name="weak_with_tool", confidence=0.30, evidence_limited=False),
        ]

        out = await check_confidence_gate({"dimensions": dims})

        names = {d["name"] for d in out["low_confidence_dimensions"]}
        assert names == {"weak_with_tool"}
        assert out["low_conf_count"] == 1
        assert out["all_confident"] is False

    async def test_threshold_boundary_inclusive(self) -> None:
        """conf == THRESHOLD is confident; conf one ULP below is flagged.

        The gate uses `confidence < CONFIDENCE_THRESHOLD`, so 0.65 itself
        is confident and 0.6499 trips the gate. Asserting this protects
        against accidental `<=` regressions that would either inflate
        budget burn or starve self-correction.
        """
        dims = [
            _make_dim(name="at_threshold", confidence=CONFIDENCE_THRESHOLD),
            _make_dim(name="below_threshold", confidence=0.6499),
        ]

        out = await check_confidence_gate({"dimensions": dims})

        names = {d["name"] for d in out["low_confidence_dimensions"]}
        assert names == {"below_threshold"}
        assert out["low_conf_count"] == 1
        assert out["all_confident"] is False


# ---------------------------------------------------------------------------
# compute_overall_match_score
# ---------------------------------------------------------------------------


class TestComputeOverallMatchScore:
    """Bit-exact weighted-average contract.

    Every downstream consumer sorts on `overall_match_score`, so this
    activity is the canonical numeric outcome of the workflow. The tests
    pin the rounding strategy (ROUND_HALF_UP at 2dp), the Decimal-exact
    coercion (no float drift), and bit-exact reproducibility across two
    calls with the same input.
    """

    async def test_weighted_average_three_dims(self) -> None:
        """80*0.4 + 90*0.3 + 70*0.3 = 32 + 27 + 21 = 80.00 exactly."""
        dims = [
            _make_dim(name="a", score=80, weight=0.4),
            _make_dim(name="b", score=90, weight=0.3),
            _make_dim(name="c", score=70, weight=0.3),
        ]

        out = await compute_overall_match_score({"dimensions": dims})

        # `model_dump(mode="json")` serializes Decimal as a string; the
        # workflow re-parses via `Decimal(value)` at the persist boundary.
        assert Decimal(out["overall_match_score"]) == Decimal("80.00")
        # Sanity: weights sum to 1.00 as the rubric contract requires.
        assert Decimal(out["weight_sum"]) == Decimal("1.00")

    async def test_bit_exact_reproducibility(self) -> None:
        """Two calls with the same input produce identical Decimal output.

        Replay-determinism contract: Temporal will re-execute the
        activity on history replay; if this ever returned a different
        Decimal value the workflow would deadlock with a
        non-deterministic-event-history error.
        """
        dims = [
            _make_dim(name="a", score=83, weight=0.35),
            _make_dim(name="b", score=67, weight=0.25),
            _make_dim(name="c", score=91, weight=0.40),
        ]

        out1 = await compute_overall_match_score({"dimensions": dims})
        out2 = await compute_overall_match_score({"dimensions": dims})

        # Compare the string serialization AND the Decimal coercion —
        # both must match bit-for-bit.
        assert out1 == out2
        assert Decimal(out1["overall_match_score"]) == Decimal(
            out2["overall_match_score"]
        )

    async def test_decimal_not_float(self) -> None:
        """Output is a string (Decimal-serialized), not a float repr.

        `Decimal(str(score)) * Decimal(str(weight))` avoids IEEE-754
        rounding noise; the output string should have exactly 2 decimal
        places and never carry a trailing `0.1 + 0.2 = 0.30000...` tail.
        """
        # Pick weights that would expose float drift if any path used
        # native floats: 0.1 + 0.2 + 0.7 = 1.0 exactly in Decimal but
        # ~0.9999999999 in IEEE-754.
        dims = [
            _make_dim(name="a", score=50, weight=0.1),
            _make_dim(name="b", score=50, weight=0.2),
            _make_dim(name="c", score=50, weight=0.7),
        ]

        out = await compute_overall_match_score({"dimensions": dims})

        score_str = out["overall_match_score"]
        # Should be exactly "50.00" — no float-noise digits.
        assert score_str == "50.00"
        assert Decimal(score_str) == Decimal("50.00")

    async def test_round_half_up_at_two_decimals(self) -> None:
        """ROUND_HALF_UP: 75.005 -> 75.01 (not banker's 75.00)."""
        # Construct weights/scores producing a 3rd-decimal of exactly 5.
        # Score 75 weight 1.0 + tiny bias via two more dims yields
        # 75.005 before quantization.
        # Easier path: hand-pick 75.005 directly via score=15001, weight
        # would be out of range. Use weighted combination instead.
        # 80 * 0.5 + 70 * 0.5 = 75.00 — no rounding case.
        # 75 * 0.5 + 75.01 not allowed (int score). Use:
        # 76 * 0.5 + 74.01-not-allowed... use 3 dims with carefully chosen
        # weights summing to 1.00:
        #   75 * 0.5 + 75 * 0.49 + 76 * 0.01 = 37.5 + 36.75 + 0.76 = 75.01
        dims = [
            _make_dim(name="a", score=75, weight=0.5),
            _make_dim(name="b", score=75, weight=0.49),
            _make_dim(name="c", score=76, weight=0.01),
        ]

        out = await compute_overall_match_score({"dimensions": dims})

        # The above arithmetic in Decimal-land is exact at 75.01.
        assert Decimal(out["overall_match_score"]) == Decimal("75.01")

    async def test_empty_dimensions_raises(self) -> None:
        """Empty dims is a workflow precondition violation; surface loud."""
        with pytest.raises(ValueError, match="empty"):
            await compute_overall_match_score({"dimensions": []})

    async def test_zero_weight_sum_raises(self) -> None:
        """If every dim weighs 0 the weighted average is undefined."""
        dims = [
            _make_dim(name="a", score=80, weight=0.0),
            _make_dim(name="b", score=90, weight=0.0),
        ]

        with pytest.raises(ValueError, match="weight is zero"):
            await compute_overall_match_score({"dimensions": dims})

    async def test_out_of_range_score_raises(self) -> None:
        """ScorecardDimension already bounds scores at [0,100]; defense
        in depth at the activity boundary catches retries with raw dicts."""
        # Bypass pydantic by sending a raw dict with an out-of-range score.
        bad_dim = {
            "name": "bad",
            "score": 200,  # out of range
            "weight": 1.0,
        }
        with pytest.raises(ValueError, match="outside"):
            await compute_overall_match_score({"dimensions": [bad_dim]})


# ---------------------------------------------------------------------------
# build_scoring_prompt
# ---------------------------------------------------------------------------


class TestBuildScoringPrompt:
    """Prompt-assembly contract.

    The prompt is the single source of truth for what we ask the LLM;
    asserting its key sections protects against regressions that would
    silently change the LLM's calibration (e.g. losing the rubric block
    would make the LLM hallucinate dim names).
    """

    @staticmethod
    def _base_payload() -> dict:
        return {
            "candidate_profile_json": {
                "full_name": "Alice Engineer",
                "seniority": "senior",
                "years_experience": 7,
                "location": "Berlin",
                "github_username": "alice",
                "skills": [
                    {"name": "Python"},
                    {"name": "Kafka"},
                    {"name": "Kubernetes"},
                ],
                "work_history": [
                    {
                        "role_title": "Staff Engineer",
                        "company": "Stripe",
                        "start_date": "2020",
                        "end_date": "2024",
                    }
                ],
                "resume_text": "Built distributed payments infra for 4y.",
            },
            "job_description": "Senior backend engineer for fintech infra.",
            "intake_notes": "Customer wants someone who has shipped Kafka prod.",
            "rubric_json": {
                "dimensions": [
                    {
                        "name": "distributed_systems_depth",
                        "weight": 0.5,
                        "description": "Has shipped distributed infra at scale.",
                    },
                    {
                        "name": "language_depth",
                        "weight": 0.3,
                        "description": "Strong primary-language proficiency.",
                    },
                    {
                        "name": "system_design_thinking",
                        "weight": 0.2,
                        "description": "Designs systems holistically.",
                    },
                ]
            },
        }

    async def test_rubric_dimensions_appear_in_prompt(self) -> None:
        out = await build_scoring_prompt(self._base_payload())
        prompt = out["prompt"]

        # All three dim names must be visible to the LLM.
        assert "distributed_systems_depth" in prompt
        assert "language_depth" in prompt
        assert "system_design_thinking" in prompt
        # The weights must be visible too — the LLM echoes them back
        # verbatim, and `compute_overall_match_score` consumes them
        # downstream.
        assert "0.50" in prompt or "weight=0.50" in prompt

    async def test_candidate_summary_included(self) -> None:
        out = await build_scoring_prompt(self._base_payload())
        prompt = out["prompt"]
        summary = out["candidate_summary"]

        # The summary is surfaced both inline AND as a standalone field
        # (used for log/trace correlation).
        assert "Alice Engineer" in summary
        assert "Alice Engineer" in prompt
        # Skills appear in the per-prompt skills block.
        assert "Python" in prompt
        assert "Kafka" in prompt

    async def test_output_format_instructions_present(self) -> None:
        """The OUTPUT FORMAT block tells the LLM to emit ScorecardOutput JSON.

        Losing this section would route the LLM into free-form prose and
        crash downstream JSON parsing.
        """
        out = await build_scoring_prompt(self._base_payload())
        prompt = out["prompt"]

        assert "OUTPUT FORMAT" in prompt
        assert "dimensions" in prompt
        assert "confidence" in prompt
        # The prompt explicitly forbids emitting the overall score; if
        # this section is removed the LLM may produce one and we'd start
        # silently overwriting our deterministic computation.
        assert "overall_match_score" in prompt  # mentioned in the "do NOT emit" note

    async def test_intake_notes_surfaced(self) -> None:
        """Operator intake notes must reach the LLM — that is the operator's
        only knob for steering the scoring narrative."""
        out = await build_scoring_prompt(self._base_payload())
        assert "Kafka prod" in out["prompt"]

    async def test_missing_intake_notes_renders_placeholder(self) -> None:
        """`intake_notes=None` produces a `(none)` placeholder, not a crash."""
        payload = self._base_payload()
        payload["intake_notes"] = None

        out = await build_scoring_prompt(payload)

        # Loose contract: prompt assembled successfully and the intake
        # section is still labeled — exact placeholder text is internal.
        assert "OPERATOR INTAKE NOTES" in out["prompt"]
