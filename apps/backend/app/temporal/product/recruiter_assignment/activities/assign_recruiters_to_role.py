"""W3.3 — `assign_recruiters_to_role` activity (Recruiter Assignment Agent).

Final commit step of the Recruiter Assignment workflow after HITL #1 approval:

  1. Validate every recruiter id exists in PG and has `status == "active"`.
     This is a security check — without it, an operator (or a bug in the
     proposal pipeline / override path) could write Assignment rows pointing
     at suspended/pending/non-existent recruiters and they would silently
     "ghost-assign" with no downstream notification path.

  2. UPSERT one row per recruiter into `assignments` using PostgreSQL
     `INSERT ... ON CONFLICT (job_id, recruiter_id) DO UPDATE`. The unique
     constraint `uq_assignments_job_recruiter` (see Assignment model) makes
     this idempotent — Temporal activity retries can safely re-run without
     creating duplicate or partial state.

  3. MERGE the corresponding `(:Recruiter)-[:ASSIGNED_TO]->(:Job)` edges in
     Neo4j. Both endpoints are merged (not matched) because the Job node
     may not exist in the graph yet — it is created lazily during candidate
     indexing in Wave 4. Cypher is fully parameterized; no string
     interpolation of payload values.

PG and Neo4j writes are sequenced (PG first, Neo4j second). They are *not*
in a single distributed transaction — that is intentional. If Neo4j fails
after PG commits, Temporal retries the activity; the PG upsert is a no-op
(ON CONFLICT branch already wrote `operator_confirmed`) and Neo4j MERGE
re-converges idempotently. The reverse order would risk graph edges to
recruiters never inserted in PG — which would corrupt the operator UI.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity

from app.core.database import async_session_maker
from app.core.neo4j_client import Neo4jClientManager
from app.database.models import Assignment, Recruiter
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"assign_recruiters_to_role: invalid UUID for {field}: {value!r}"
        ) from exc


def _coerce_score(value: object) -> Decimal | None:
    """Coerce LLM-provided score (int|float|str|None) to Decimal | None.

    `ai_score` is `Numeric(5,2)` in PG; passing a Python float trips
    asyncpg in some configurations. Normalising to Decimal avoids that.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _coerce_confidence(value: object) -> Decimal | None:
    """Coerce confidence (0.0–1.0 float) to Numeric(3,2)-safe Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@ActivityRegistry.register("recruiter_assignment", "assign_recruiters_to_role")
@activity.defn(name="recruiter_assignment.assign_recruiters_to_role")
async def assign_recruiters_to_role(payload: dict) -> dict:
    """Persist operator-confirmed recruiter assignments to PG + Neo4j.

    Inputs (dict):
        job_id: str — UUID of the Job row.
        confirmed_recruiter_ids: list[str] — final operator-approved set
            (already merged from AI proposal + override_set upstream).
        fit_scores: list[dict] — RecruiterFitScore.model_dump() entries;
            used to populate `ai_score`, `ai_rationale`, `confidence` on
            assignments where the recruiter is in the AI proposal. For
            override-only recruiters, all three fields default to NULL.
        operator_id: str — UUID of the Operator who approved the proposal.
        operator_override: bool — True iff the override_set path was used
            (operator picked recruiter(s) outside the AI proposal). Stored
            on every row in this batch so Agent 0 telemetry can compute
            override rate.

    Returns:
        {
          "assigned_count": int,
          "assignment_ids": list[str],
          "job_id": str,
        }
    """
    # ---- payload validation -------------------------------------------------
    job_id_raw = payload.get("job_id")
    confirmed_ids_raw = payload.get("confirmed_recruiter_ids")
    fit_scores_raw = payload.get("fit_scores")
    operator_id_raw = payload.get("operator_id")
    operator_override_raw = payload.get("operator_override")

    if not isinstance(job_id_raw, str) or not job_id_raw.strip():
        raise ValueError(
            "assign_recruiters_to_role: 'job_id' is required (str UUID)"
        )
    if not isinstance(confirmed_ids_raw, list) or not confirmed_ids_raw:
        raise ValueError(
            "assign_recruiters_to_role: 'confirmed_recruiter_ids' must be a "
            "non-empty list[str]"
        )
    if not all(isinstance(rid, str) and rid.strip() for rid in confirmed_ids_raw):
        raise ValueError(
            "assign_recruiters_to_role: every confirmed_recruiter_ids entry "
            "must be a non-empty string UUID"
        )
    if not isinstance(fit_scores_raw, list):
        raise ValueError(
            "assign_recruiters_to_role: 'fit_scores' must be a list[dict] "
            "(may be empty for pure-override decisions)"
        )
    if not isinstance(operator_id_raw, str) or not operator_id_raw.strip():
        raise ValueError(
            "assign_recruiters_to_role: 'operator_id' is required (str UUID)"
        )
    if not isinstance(operator_override_raw, bool):
        raise ValueError(
            "assign_recruiters_to_role: 'operator_override' must be a bool"
        )

    job_uuid = _parse_uuid(job_id_raw, "job_id")
    operator_uuid = _parse_uuid(operator_id_raw, "operator_id")

    # De-duplicate confirmed_recruiter_ids while preserving order — operator
    # UI / override merge upstream may legitimately produce duplicates if the
    # same recruiter appears in both the proposal and the override_set.
    seen_ids: set[str] = set()
    confirmed_ids: list[str] = []
    for rid in confirmed_ids_raw:
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        confirmed_ids.append(rid)

    confirmed_uuids: list[uuid.UUID] = [
        _parse_uuid(rid, f"confirmed_recruiter_ids[{idx}]")
        for idx, rid in enumerate(confirmed_ids)
    ]

    # Index fit scores by recruiter_id for O(1) lookup. Skip malformed entries
    # (defensive — RecruiterFitScore is validated upstream by the agent loop).
    fit_score_by_id: dict[str, dict] = {}
    for entry in fit_scores_raw:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("recruiter_id")
        if isinstance(rid, str) and rid:
            fit_score_by_id[rid] = entry

    LOGGER.info(
        "Assigning recruiters to role",
        extra={
            "job_id": job_id_raw,
            "operator_id": operator_id_raw,
            "operator_override": operator_override_raw,
            "recruiter_count": len(confirmed_ids),
            "fit_score_count": len(fit_score_by_id),
        },
    )

    now = datetime.now(UTC)
    assignment_ids: list[str] = []

    # ---- 1. Recruiter existence + active-status check + UPSERT --------------
    async with async_session_maker() as session:
        # Security: every recruiter_id must (a) exist and (b) be active.
        # A single SELECT fetches the active subset; any missing id is then
        # diagnosed below with a precise error message.
        existing_result = await session.execute(
            select(Recruiter.id, Recruiter.status).where(
                Recruiter.id.in_(confirmed_uuids)
            )
        )
        status_by_id: dict[str, str] = {
            str(row.id): str(row.status) for row in existing_result.all()
        }

        missing: list[str] = [
            rid for rid in confirmed_ids if rid not in status_by_id
        ]
        non_active: list[tuple[str, str]] = [
            (rid, status_by_id[rid])
            for rid in confirmed_ids
            if rid in status_by_id and status_by_id[rid] != "active"
        ]
        if missing or non_active:
            raise ValueError(
                "assign_recruiters_to_role: invalid recruiter set — "
                f"missing={missing!r}, non_active={non_active!r}. "
                "Operator-confirmed recruiters must all exist and be active; "
                "refusing to write ghost assignments."
            )

        # UPSERT one row per recruiter. ON CONFLICT path makes this idempotent
        # under Temporal activity retry: a previous successful run will have
        # written the row with status='operator_confirmed' already; a partial
        # run from before HITL approval may have left status='recommended',
        # so we always overwrite status, confirmed_at, confirmed_by_operator_id.
        for rid, ruuid in zip(confirmed_ids, confirmed_uuids, strict=True):
            score_entry = fit_score_by_id.get(rid)
            ai_score: Decimal | None = None
            ai_rationale: str | None = None
            confidence: Decimal | None = None
            if score_entry is not None:
                ai_score = _coerce_score(score_entry.get("score"))
                rationale_val = score_entry.get("rationale")
                ai_rationale = (
                    rationale_val if isinstance(rationale_val, str) else None
                )
                confidence = _coerce_confidence(score_entry.get("confidence"))

            stmt = (
                pg_insert(Assignment)
                .values(
                    job_id=job_uuid,
                    recruiter_id=ruuid,
                    ai_score=ai_score,
                    ai_rationale=ai_rationale,
                    confidence=confidence,
                    operator_override=operator_override_raw,
                    status="operator_confirmed",
                    confirmed_by_operator_id=operator_uuid,
                    confirmed_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["job_id", "recruiter_id"],
                    set_={
                        "status": "operator_confirmed",
                        "confirmed_at": now,
                        "confirmed_by_operator_id": operator_uuid,
                    },
                )
                .returning(Assignment.id)
            )
            result = await session.execute(stmt)
            assignment_id = result.scalar_one()
            assignment_ids.append(str(assignment_id))

        await session.commit()

    # ---- 2. Neo4j ASSIGNED_TO edge merges -----------------------------------
    # Job node is MERGEd because candidate-side workflows haven't necessarily
    # created it yet in this environment. Recruiter node is MATCHed — if it
    # doesn't exist in the graph, the indexing pipeline missed it; we let the
    # MATCH miss silently rather than fabricate a Recruiter node here, since
    # the PG row IS the source of truth and the operator UI reads from PG.
    cypher = """
    MERGE (j:Job {id: $job_id})
    WITH j
    MATCH (r:Recruiter {id: $recruiter_id})
    MERGE (r)-[rel:ASSIGNED_TO]->(j)
    SET rel.status = "active",
        rel.assigned_at = $now,
        rel.confirmed_by_operator = true
    """
    now_iso = now.isoformat()
    async with await Neo4jClientManager.get_session() as graph_session:
        for rid in confirmed_ids:
            await graph_session.run(
                cypher,
                job_id=job_id_raw,
                recruiter_id=rid,
                now=now_iso,
            )

    LOGGER.info(
        "Recruiters assigned",
        extra={
            "job_id": job_id_raw,
            "operator_id": operator_id_raw,
            "operator_override": operator_override_raw,
            "assigned_count": len(assignment_ids),
            "assignment_ids": assignment_ids,
        },
    )

    return {
        "assigned_count": len(assignment_ids),
        "assignment_ids": assignment_ids,
        "job_id": job_id_raw,
    }
