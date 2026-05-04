"""C2 — `generate_evaluation_rubric` activity.

LLM produces a 4-8 dimension weighted rubric for the role. Because
`RubricDimension.name` enforces a strict `^[a-z][a-z0-9_]*$` regex that
rejects natural-language phrases like "Distributed Systems Depth", we cannot
hand the raw LLM JSON straight to `structured_complete` — Pydantic would 422
on otherwise-recoverable output.

Approach: call `complete` for raw JSON, normalize names + weights in the
activity body, then construct the `EvaluationRubric` manually. Mirrors the
`infer_skill_depth` pattern (raw JSON parse + manual model build) used by
candidate_indexing.

Replay determinism guards:
  * names: lowercase + non-alnum → `_`, collapsed underscores, leading-digit fix.
  * weights: renormalize to sum=1.0 (warn-log if drift > 0.05 — design D6).
  * dimensions: truncate to top 8 by weight; raise ValueError if < 4.
  * sort: deterministic `(-weight, name)` so replay produces identical bytes.

Per CLAUDE.md AI/LLM rules: user content (classification JSON, intake notes,
extra hints) is delimited inside the `user` role; the privileged system prompt
never inlines untrusted text.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError
from temporalio import activity

from app.core.llm import LLMMessage, get_llm_client
from app.schemas.product.job import EvaluationRubric, RubricDimension
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_MIN_DIMENSIONS = 4
_MAX_DIMENSIONS = 8
_WEIGHT_DRIFT_THRESHOLD = 0.05

_NAME_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NAME_COLLAPSE_UNDERSCORE = re.compile(r"_+")
_FENCE_PREFIX = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_SUFFIX = re.compile(r"\s*```\s*$")


_SYSTEM_PROMPT = """You design weighted evaluation rubrics for hiring at a managed
recruiting service. Output strict JSON matching this schema:

{
  "dimensions": [
    {
      "name": "lowercase_snake_case_identifier",
      "description": "1-2 sentences (<= 500 chars).",
      "weight": 0.15,                 // 0.0-1.0; weights across dimensions sum to ~1.0
      "evaluation_guidance": "How a reviewer scores this dimension (<= 1000 chars)."
    }
  ],
  "rationale": "Why these dimensions and these weights (<= 1000 chars)."
}

Rules:
- Produce between 4 and 8 dimensions, no more, no less. Choose what's most
  predictive for the role; do not pad to hit a number.
- Names MUST be lowercase_snake_case (we will sanitize, but try to comply).
- Weights are floats in [0, 1] and should sum to ~1.0 (we renormalize).
- The rubric is candidate-agnostic — it describes WHAT to evaluate, not
  any specific candidate.
- Calibrate weights to the role classification + intake notes provided.
"""


def _normalize_name(raw: str) -> str:
    """Coerce a free-text dimension name into `lowercase_snake_case`.

    LLMs commonly return "Distributed Systems Depth" or "OSS / Open Source".
    `RubricDimension.name` enforces `^[a-z][a-z0-9_]*$`, so we sanitize before
    constructing the model rather than letting validation 422 the workflow.
    """
    if not isinstance(raw, str):
        raise ValueError("dimension name must be a string")
    cleaned = _NAME_NON_ALNUM.sub("_", raw.strip().lower())
    cleaned = _NAME_COLLAPSE_UNDERSCORE.sub("_", cleaned).strip("_")
    if not cleaned:
        raise ValueError(f"dimension name normalized to empty: {raw!r}")
    if cleaned[0].isdigit():
        cleaned = f"d_{cleaned}"
    return cleaned


def _strip_code_fences(raw: str) -> str:
    """Remove ```json ... ``` wrappers some models emit even when asked for JSON."""
    stripped = raw.strip()
    stripped = _FENCE_PREFIX.sub("", stripped)
    stripped = _FENCE_SUFFIX.sub("", stripped)
    return stripped.strip()


def _build_user_prompt(
    classification: dict[str, Any],
    intake_notes: str | None,
    extra: dict[str, Any] | None,
) -> str:
    notes = intake_notes if intake_notes else "(none)"
    extra_json = json.dumps(extra, sort_keys=True) if extra else "(none)"
    return (
        "<<<ROLE_CLASSIFICATION>>>\n"
        f"{json.dumps(classification, sort_keys=True)}\n"
        "<<<END ROLE_CLASSIFICATION>>>\n\n"
        "<<<INTAKE_NOTES>>>\n"
        f"{notes}\n"
        "<<<END INTAKE_NOTES>>>\n\n"
        "<<<EXTRA_HINTS>>>\n"
        f"{extra_json}\n"
        "<<<END EXTRA_HINTS>>>\n"
    )


def _normalize_dimensions(raw_dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize names, dedupe by name (keep highest weight), enforce shape.

    Returns a list of dicts ready to feed `RubricDimension(**d)`.
    """
    if not isinstance(raw_dimensions, list):
        raise ValueError("'dimensions' must be a list in LLM output")

    by_name: dict[str, dict[str, Any]] = {}
    for entry in raw_dimensions:
        if not isinstance(entry, dict):
            raise ValueError("each dimension must be a JSON object")
        name = _normalize_name(entry.get("name", ""))
        try:
            weight = float(entry.get("weight", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"dimension {name!r}: weight is not numeric") from exc
        if weight < 0.0:
            weight = 0.0
        if weight > 1.0:
            weight = 1.0

        normalized = {
            "name": name,
            "description": str(entry.get("description") or "").strip(),
            "weight": weight,
            "evaluation_guidance": str(entry.get("evaluation_guidance") or "").strip(),
        }
        # Backfill empty description/guidance to satisfy min_length=1 validators.
        if not normalized["description"]:
            normalized["description"] = f"Evaluates {name.replace('_', ' ')}."
        if not normalized["evaluation_guidance"]:
            normalized["evaluation_guidance"] = (
                f"Score 0-5 based on demonstrated {name.replace('_', ' ')}."
            )

        # Dedupe by name — LLMs occasionally repeat. Keep highest-weight copy.
        existing = by_name.get(name)
        if existing is None or normalized["weight"] > existing["weight"]:
            by_name[name] = normalized

    return list(by_name.values())


@ActivityRegistry.register("job_intake", "generate_evaluation_rubric")
@activity.defn(name="job_intake.generate_evaluation_rubric")
async def generate_evaluation_rubric(payload: dict) -> dict:
    """Produce a 4-8 dimension weighted rubric for the classified role.

    Inputs (dict):
        classification: dict — the `RoleClassification` JSON from C1.
        intake_notes: str | None — operator's onboarding notes (D8).
        extra: dict | None — pass-through hints (e.g. company-side rubric tweaks).

    Returns:
        `EvaluationRubric.model_dump(mode="json")` with normalized names,
        renormalized weights summing to 1.0, deterministically sorted.
    """
    classification = payload.get("classification")
    if not isinstance(classification, dict):
        raise ValueError(
            "generate_evaluation_rubric: 'classification' is required (dict)"
        )
    intake_notes = payload.get("intake_notes")
    if intake_notes is not None and not isinstance(intake_notes, str):
        raise ValueError("'intake_notes' must be a string when provided")
    extra = payload.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError("'extra' must be a dict when provided")

    LOGGER.info(
        "Generating evaluation rubric",
        extra={
            "role_category": classification.get("role_category"),
            "seniority_level": classification.get("seniority_level"),
            "intake_notes_present": bool(intake_notes),
            "extra_present": bool(extra),
        },
    )

    llm = get_llm_client()
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=_build_user_prompt(classification, intake_notes, extra),
        ),
    ]

    try:
        response = await llm.complete(messages=messages, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 — bubble up for Temporal _LLM_RETRY
        LOGGER.error(
            "generate_evaluation_rubric LLM call failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
        raise

    raw_text = _strip_code_fences(response.content)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        LOGGER.error(
            "generate_evaluation_rubric: LLM output is not valid JSON",
            extra={"error": str(exc)},
        )
        raise ValueError("LLM rubric output is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ValueError("LLM rubric output must be a JSON object")

    rationale = str(parsed.get("rationale") or "").strip()
    if not rationale:
        rationale = "Auto-generated rubric; rationale was not provided by the model."

    dimensions = _normalize_dimensions(parsed.get("dimensions", []))

    # Truncate to top _MAX_DIMENSIONS by weight (deterministic tie-break: name asc).
    dimensions.sort(key=lambda d: (-d["weight"], d["name"]))
    if len(dimensions) > _MAX_DIMENSIONS:
        LOGGER.warning(
            "Rubric exceeded max dimensions; truncating",
            extra={"received": len(dimensions), "kept": _MAX_DIMENSIONS},
        )
        dimensions = dimensions[:_MAX_DIMENSIONS]

    if len(dimensions) < _MIN_DIMENSIONS:
        raise ValueError(
            f"rubric must have at least {_MIN_DIMENSIONS} dimensions, "
            f"got {len(dimensions)}"
        )

    # Renormalize weights to sum=1.0; warn on drift > threshold (D6).
    total_weight = sum(d["weight"] for d in dimensions)
    if total_weight <= 0:
        # Degenerate output — assign uniform weights.
        LOGGER.warning(
            "Rubric weights sum to zero; assigning uniform weights",
            extra={"dimension_count": len(dimensions)},
        )
        uniform = round(1.0 / len(dimensions), 6)
        for dim in dimensions:
            dim["weight"] = uniform
    else:
        drift = abs(1.0 - total_weight)
        if drift > _WEIGHT_DRIFT_THRESHOLD:
            LOGGER.warning(
                "Rubric weight drift exceeded threshold; renormalizing",
                extra={
                    "weight_drift": round(drift, 4),
                    "total_weight": round(total_weight, 4),
                    "threshold": _WEIGHT_DRIFT_THRESHOLD,
                },
            )
        for dim in dimensions:
            dim["weight"] = round(dim["weight"] / total_weight, 6)

    # Deterministic sort post-normalization for replay equality.
    dimensions.sort(key=lambda d: (-d["weight"], d["name"]))

    try:
        rubric_dimensions = [RubricDimension(**d) for d in dimensions]
        rubric = EvaluationRubric(dimensions=rubric_dimensions, rationale=rationale)
    except ValidationError as exc:
        LOGGER.error(
            "generate_evaluation_rubric: post-normalization validation failed",
            extra={"error": str(exc)},
        )
        raise

    LOGGER.info(
        "Rubric generated",
        extra={
            "dimension_count": len(rubric.dimensions),
            "names": [d.name for d in rubric.dimensions],
        },
    )

    return rubric.model_dump(mode="json")
