"""Activity: score_recruiter_fit.

LLM-driven fit scoring for the Recruiter Assignment Agent (Agent 0). Given a
batch of recruiter candidates and the role context (category, seniority, stage,
must-have skills), produce a `RecruiterFitScore` per recruiter using a single
batched structured-output LLM call.

Key design points (per spec §16.7 and the recruiter_assignment plan):

  * **Memoization** — `payload.memo` carries already-scored recruiters from
    prior turns of the agent loop. We split the input IDs into `to_score` (need
    the LLM) and `cached` (return as-is). If `to_score` is empty we skip the
    LLM call entirely. This keeps the agent's per-iteration cost flat as the
    pool grows or the operator rejects and re-loops.

  * **Batched call** — at most 10 recruiters per call; we ValueError above 10
    rather than silently truncate. The Recruiter Assignment plan caps the
    proposed set at top_n<=10, so this matches the workflow's invariants.

  * **Defensive defaults** — if the LLM omits a recruiter_id from its response
    (rare but possible with structured outputs and large batches), we
    synthesize a low-confidence neutral score (50, 0.3) instead of erroring
    out. Losing one recruiter from a 10-row batch shouldn't fail the workflow;
    the operator UI can surface low-confidence rows for review.

  * **LLM trust boundary** — the privileged system prompt is static; recruiter
    profiles and role data are delimited inside the user role with `<<<…>>>`
    fences (per CLAUDE.md AI/LLM rules). LLM output is parsed via Pydantic
    `structured_complete` and validated against the workflow's
    `RecruiterFitScore` shape — never passed to eval/SQL/innerHTML.

  * **Replay determinism** — recruiter rows are sorted by id before serializing
    into the prompt, and the merged result is sorted by recruiter_id, so two
    identical inputs produce byte-identical activity output for Temporal
    history equality.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from temporalio import activity

from app.core.database import async_session_maker
from app.core.llm import LLMMessage, get_llm_client
from app.database.models import Recruiter
from app.schemas.product.recruiter_assignment import RecruiterFitScore, SubScores
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_MAX_BATCH_SIZE = 10
_DEFAULT_SCORE = 50
_DEFAULT_CONFIDENCE = 0.3
_DEFAULT_RATIONALE = "Score unavailable"
_DEFAULT_SUB_SCORE = 50


class BatchFitScores(BaseModel):
    """Batch wrapper used as the structured-output schema for the LLM call.

    The LLM returns one `RecruiterFitScore` per recruiter in the batch.
    Defined locally (not in `app.schemas.product.recruiter_assignment`) because
    it is an LLM IO concern, not a workflow IO concern — the workflow stores
    individual `RecruiterFitScore` rows, never the wrapper.
    """

    scores: list[RecruiterFitScore]


_SYSTEM_PROMPT = (
    "You score recruiter-to-role fit for a managed recruiting service.\n"
    "For each recruiter in the batch, output a RecruiterFitScore.\n"
    "Score 0-100. Confidence 0-1. Rationale max 200 chars.\n"
    "Sub-scores: domain (domain expertise match), stage (company stage experience),\n"
    "seniority (tracks correct seniority level), fill_rate (placement success rate),\n"
    "close_time (speed to close - higher fill rate / lower avg days = higher score).\n"
    "Return JSON matching BatchFitScores schema exactly."
)


def _to_float(value: Any) -> float | None:
    """Coerce numeric (int | float | Decimal | None) to float | None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_stage_history(extra: dict | None) -> list[str]:
    """Pull `stage_history` from the Recruiter.extra JSONB blob, defensively.

    The shape of `extra` is loosely typed; we only surface a list-of-strings
    summary. Anything else is treated as missing.
    """
    if not isinstance(extra, dict):
        return []
    raw = extra.get("stage_history")
    if not isinstance(raw, list):
        return []
    return sorted({str(item) for item in raw if item is not None})


def _build_recruiter_profile(row: Recruiter) -> dict[str, Any]:
    """Project a `Recruiter` ORM row to the JSON shape we hand to the LLM.

    Kept tiny on purpose — we only feed signals the rubric mentions
    (domain, stage, seniority-via-history, fill_rate, close_time). Bio,
    LinkedIn URL, and similar PII-adjacent fields are intentionally omitted.
    """
    return {
        "recruiter_id": str(row.id),
        "full_name": row.full_name,
        "domain_expertise": sorted(row.domain_expertise or []),
        "total_placements": int(row.total_placements or 0),
        "avg_days_to_close": row.avg_days_to_close,
        "fill_rate_pct": _to_float(row.fill_rate_pct),
        "stage_history": _extract_stage_history(row.extra),
    }


def _build_user_prompt(
    *,
    role_category: str,
    seniority_level: str,
    stage_fit: str | None,
    must_have_skills: list[str],
    recruiter_profiles: list[dict[str, Any]],
) -> str:
    """Assemble the delimited user prompt.

    Role data (already validated upstream) and recruiter profiles (sourced from
    our own DB, not user input) are placed inside `<<<…>>>` fences. There is no
    system-role string interpolation, satisfying the AI/LLM trust boundary.
    """
    return (
        "<<<ROLE>>>\n"
        f"Category: {role_category}\n"
        f"Seniority: {seniority_level}\n"
        f"Stage: {stage_fit if stage_fit else 'any'}\n"
        f"Must-have skills: {json.dumps(must_have_skills, sort_keys=False)}\n"
        "<<<END_ROLE>>>\n\n"
        "<<<RECRUITERS>>>\n"
        f"{json.dumps(recruiter_profiles, sort_keys=True, default=str)}\n"
        "<<<END_RECRUITERS>>>\n\n"
        "Score each recruiter. Return a score object for every recruiter_id listed."
    )


def _default_score(recruiter_id: str) -> RecruiterFitScore:
    """Neutral fallback when the LLM omits a recruiter from its response."""
    return RecruiterFitScore(
        recruiter_id=recruiter_id,
        score=_DEFAULT_SCORE,
        confidence=_DEFAULT_CONFIDENCE,
        rationale=_DEFAULT_RATIONALE,
        sub_scores=SubScores(
            domain=_DEFAULT_SUB_SCORE,
            stage=_DEFAULT_SUB_SCORE,
            seniority=_DEFAULT_SUB_SCORE,
            fill_rate=_DEFAULT_SUB_SCORE,
            close_time=_DEFAULT_SUB_SCORE,
        ),
    )


def _coerce_memo_entry(recruiter_id: str, raw: Any) -> RecruiterFitScore:
    """Validate a memo entry into a `RecruiterFitScore`.

    Memo originates from prior runs of this same activity, so the shape should
    match — but we still validate (cheap) and fall back to a default rather
    than crash the workflow if a malformed dict slipped through.
    """
    if isinstance(raw, RecruiterFitScore):
        return raw
    if not isinstance(raw, dict):
        LOGGER.warning(
            "Memo entry is not a dict; using default score",
            extra={"recruiter_id": recruiter_id},
        )
        return _default_score(recruiter_id)
    try:
        return RecruiterFitScore.model_validate(raw)
    except ValidationError as exc:
        LOGGER.warning(
            "Memo entry failed validation; using default score",
            extra={"recruiter_id": recruiter_id, "error": str(exc)},
        )
        return _default_score(recruiter_id)


@ActivityRegistry.register("recruiter_assignment", "score_recruiter_fit")
@activity.defn(name="recruiter_assignment.score_recruiter_fit")
async def score_recruiter_fit(payload: dict) -> dict:
    """Score recruiter-to-role fit for a batch of up to 10 recruiters.

    Args:
        payload: dict with keys
            - recruiter_ids (list[str]): up to 10 recruiter UUID strings.
            - role_category (str): e.g. 'engineering', 'gtm'.
            - seniority_level (str): e.g. 'staff', 'senior'.
            - stage_fit (str | None): target company stage; 'any' if missing.
            - must_have_skills (list[str]): role's must-have skills.
            - memo (dict[str, dict]): {recruiter_id: RecruiterFitScore-dict}
              of already-scored recruiters; these are returned unchanged and
              never re-sent to the LLM (per spec §16.7 memoization).

    Returns:
        dict with keys
            - scores (list[dict]): RecruiterFitScore.model_dump(mode='json')
              for every requested recruiter, sorted by recruiter_id.
            - scored_count (int): number freshly scored via the LLM.
            - cached_count (int): number returned from `memo` without an LLM call.

    Raises:
        ValueError: input validation failures, or `to_score` exceeding 10.
    """
    # ---- 1. Parse + validate payload ---------------------------------------
    raw_ids = payload.get("recruiter_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("payload.recruiter_ids must be a non-empty list[str]")
    # Dedupe while preserving order; reject empty strings.
    recruiter_ids: list[str] = []
    seen: set[str] = set()
    for rid in raw_ids:
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError(f"recruiter_id is not a non-empty string: {rid!r}")
        if rid in seen:
            continue
        seen.add(rid)
        recruiter_ids.append(rid)

    role_category = payload.get("role_category")
    if not isinstance(role_category, str) or not role_category:
        raise ValueError("payload.role_category is required (non-empty str)")
    seniority_level = payload.get("seniority_level")
    if not isinstance(seniority_level, str) or not seniority_level:
        raise ValueError("payload.seniority_level is required (non-empty str)")
    stage_fit = payload.get("stage_fit")
    if stage_fit is not None and not isinstance(stage_fit, str):
        raise ValueError("payload.stage_fit must be a string when provided")
    must_have_skills = payload.get("must_have_skills") or []
    if not isinstance(must_have_skills, list) or not all(
        isinstance(s, str) for s in must_have_skills
    ):
        raise ValueError("payload.must_have_skills must be list[str]")
    memo_raw = payload.get("memo") or {}
    if not isinstance(memo_raw, dict):
        raise ValueError("payload.memo must be a dict[str, RecruiterFitScore-dict]")

    # ---- 2. Split into to_score / cached -----------------------------------
    cached: dict[str, RecruiterFitScore] = {}
    to_score: list[str] = []
    for rid in recruiter_ids:
        if rid in memo_raw:
            cached[rid] = _coerce_memo_entry(rid, memo_raw[rid])
        else:
            to_score.append(rid)

    LOGGER.info(
        "score_recruiter_fit: split",
        extra={
            "requested": len(recruiter_ids),
            "to_score": len(to_score),
            "cached": len(cached),
        },
    )

    # Hard cap on LLM batch size — workflow contract is top_n<=10.
    if len(to_score) > _MAX_BATCH_SIZE:
        raise ValueError(
            f"score_recruiter_fit supports at most {_MAX_BATCH_SIZE} fresh "
            f"recruiters per call; got {len(to_score)}"
        )

    # Fast path: nothing to score, return memo verbatim.
    if not to_score:
        merged = sorted(cached.values(), key=lambda s: s.recruiter_id)
        return {
            "scores": [s.model_dump(mode="json") for s in merged],
            "scored_count": 0,
            "cached_count": len(cached),
        }

    # ---- 3. Fetch recruiter rows for to_score ------------------------------
    to_score_uuids: list[uuid.UUID] = []
    invalid_ids: list[str] = []
    for rid in to_score:
        try:
            to_score_uuids.append(uuid.UUID(rid))
        except (ValueError, TypeError):
            invalid_ids.append(rid)
    if invalid_ids:
        raise ValueError(
            f"recruiter_ids contains non-UUID values: {invalid_ids!r}"
        )

    rows_by_id: dict[str, Recruiter] = {}
    async with async_session_maker() as session:
        result = await session.execute(
            select(Recruiter).where(Recruiter.id.in_(to_score_uuids))
        )
        for row in result.scalars().all():
            rows_by_id[str(row.id)] = row

    # Recruiters requested but missing in PG get a default score — don't
    # block the workflow on graph/PG drift.
    missing_in_db = [rid for rid in to_score if rid not in rows_by_id]
    if missing_in_db:
        LOGGER.warning(
            "score_recruiter_fit: recruiters missing in PG; defaulting",
            extra={"missing_count": len(missing_in_db)},
        )

    # Sorted-by-id profile list → replay-deterministic prompt bytes.
    recruiter_profiles = [
        _build_recruiter_profile(rows_by_id[rid])
        for rid in sorted(rows_by_id.keys())
    ]

    # ---- 4. Call the LLM (only for recruiters we actually have data on) ----
    llm_scores: dict[str, RecruiterFitScore] = {}
    if recruiter_profiles:
        llm = get_llm_client()
        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=_build_user_prompt(
                    role_category=role_category,
                    seniority_level=seniority_level,
                    stage_fit=stage_fit,
                    must_have_skills=must_have_skills,
                    recruiter_profiles=recruiter_profiles,
                ),
            ),
        ]

        try:
            batch = await llm.structured_complete(
                messages=messages,
                schema=BatchFitScores,
            )
        except Exception as exc:  # noqa: BLE001 — bubble up for Temporal retry
            LOGGER.error(
                "score_recruiter_fit LLM call failed",
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "batch_size": len(recruiter_profiles),
                },
            )
            raise

        for entry in batch.scores:
            # Defensive: only accept scores for recruiters we asked about.
            if entry.recruiter_id in rows_by_id:
                llm_scores[entry.recruiter_id] = entry
            else:
                LOGGER.warning(
                    "LLM returned score for unknown recruiter_id; ignoring",
                    extra={"recruiter_id": entry.recruiter_id},
                )

    # ---- 5. Fill in defaults for any to_score id without a score -----------
    fresh_scores: dict[str, RecruiterFitScore] = {}
    for rid in to_score:
        score = llm_scores.get(rid)
        if score is None:
            LOGGER.warning(
                "No LLM score for recruiter; using default",
                extra={"recruiter_id": rid},
            )
            score = _default_score(rid)
        fresh_scores[rid] = score

    # ---- 6. Merge cached + fresh, sort, return -----------------------------
    merged_map: dict[str, RecruiterFitScore] = {**cached, **fresh_scores}
    merged = sorted(merged_map.values(), key=lambda s: s.recruiter_id)

    LOGGER.info(
        "score_recruiter_fit: complete",
        extra={
            "scored_count": len(fresh_scores),
            "cached_count": len(cached),
            "total": len(merged),
        },
    )

    return {
        "scores": [s.model_dump(mode="json") for s in merged],
        "scored_count": len(fresh_scores),
        "cached_count": len(cached),
    }
