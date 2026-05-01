"""Job intake API endpoint (Phase E1).

POST /api/v1/jobs/intake — managed job intake submission. Resolves the
authenticated user to either an active `Operator` (operator-on-behalf-of)
or a `CompanyUser` seated at the target company (hiring-manager portal),
INSERTs a `Job` row with `status="intake"`, and fires `JobIntakeWorkflow`
fire-and-forget on the Temporal `converio-queue` with
`WorkflowIDReusePolicy.REJECT_DUPLICATE` (D3).

Request / response shapes are imported from `app.schemas.generated.jobs`
(codegen output of `app/api/v1/specs/jobs.json`); the spec is the source
of truth.
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDReusePolicy

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.rate_limit import job_intake_rate_limiter
from app.core.temporal_client import get_temporal_client
from app.database.models import CompanyUser, Operator
from app.repositories.companies import CompanyRepository
from app.repositories.jobs import JobRepository
from app.repositories.operators import OperatorRepository
from app.schemas.enums import JobStatus, OperatorStatus
from app.schemas.generated.jobs import (
    JobIntakeAcceptedResponse,
    JobIntakeRequest,
    JobStatus as SpecJobStatus,
)
from app.schemas.product.job import JobIntakeInput
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)

# Temporal task queue shared by all product workflows.
_TASK_QUEUE: str = "converio-queue"

# Type alias for the actor kind discriminator used in structured logs.
_ActorKind = Literal["operator", "company_user"]


async def _resolve_actor(
    session: AsyncSession,
    current_user: CurrentUser,
    target_company_id: UUID,
) -> tuple[_ActorKind, Operator | CompanyUser]:
    """Resolve `current_user` to an `Operator` OR a `CompanyUser` row.

    Resolution order:
        1. Active `Operator` row keyed by `supabase_user_id`.
           Operators may submit on behalf of any company.
        2. `CompanyUser` row keyed by `supabase_user_id` AND seated at
           `target_company_id`. A user seated at a different company is
           explicitly rejected (403) — silently allowing it would let any
           seated hiring-manager submit intakes against arbitrary
           companies, defeating tenant-style isolation.

    Raises:
        HTTPException 403 if neither resolution path matches.

    Detail strings are intentionally generic (no echo of supplied
    `company_id` or `user_id`) so probing endpoints cannot distinguish
    "no operator row" / "wrong company" / "no seat" from the response.
    """
    # 1. Operator path — privileged, matches first.
    operator = await OperatorRepository(session).get_by_supabase_id(current_user.id)
    if operator is not None and operator.status == OperatorStatus.ACTIVE.value:
        return "operator", operator

    # 2. CompanyUser path — must be seated at the target company.
    result = await session.execute(
        select(CompanyUser).where(CompanyUser.supabase_user_id == current_user.id)
    )
    company_user = result.scalar_one_or_none()
    if company_user is not None:
        if company_user.company_id != target_company_id:
            LOGGER.warning(
                "Job intake forbidden: company mismatch",
                extra={
                    "user_id": current_user.id,
                    "company_user_id": str(company_user.id),
                    "seated_company_id": str(company_user.company_id),
                    "requested_company_id": str(target_company_id),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this company",
            )
        return "company_user", company_user

    LOGGER.warning(
        "Job intake forbidden: no actor row",
        extra={
            "user_id": current_user.id,
            "requested_company_id": str(target_company_id),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized for this company",
    )


@router.post(
    "/intake",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a managed job intake and fire JobIntakeWorkflow",
    operation_id="submit_job_intake",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Caller not authorized for the target company."},
        status.HTTP_404_NOT_FOUND: {"description": "Company not found."},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Rate limit exceeded for this company (10/hour).",
        },
    },
)
async def submit_job_intake(
    request: Request,
    payload: JobIntakeRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Accept a managed job intake and fire `JobIntakeWorkflow` fire-and-forget.

    Returns 202 with `{job_id, workflow_id, status: "intake"}` so clients
    can poll / subscribe to workflow progress. The Job row is committed
    before the workflow is started; if the Temporal start fails we surface
    a generic 500. The Job row is *not* rolled back in that case — Temporal
    has `REJECT_DUPLICATE` on the deterministic `job-intake-{id}` workflow
    id, so a retry against the same job would no-op rather than spawn a
    duplicate run. (A future PR can add a sweep job to retry orphaned
    intake rows; out of scope here per the plan.)

    Auth: any authenticated Supabase user. The caller is resolved to
    either an active `Operator` OR a `CompanyUser` seated at
    `payload.company_id`. Anyone else gets a generic 403.

    Logging: `jd_text` and `intake_notes` are PII / company-confidential
    and are NEVER written to logs. Only the lengths are logged for
    operational signal.
    """
    # 1. Resolve actor (403 on miss).
    actor_kind, actor = await _resolve_actor(session, current_user, payload.company_id)

    # 2. Verify the target company exists (404 on miss). Note: we resolve
    #    actor first so a request from an unauthorized caller does not leak
    #    company existence via the 404 vs 403 distinction.
    company_repo = CompanyRepository(session)
    company = await company_repo.get_by_id(payload.company_id)
    if company is None:
        LOGGER.info(
            "Job intake: company not found",
            extra={
                "user_id": current_user.id,
                "actor_kind": actor_kind,
                "actor_id": str(actor.id),
                "requested_company_id": str(payload.company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    # 3. Rate limit (10/hour/company_id). Single-process; see
    #    `app.core.rate_limit` docstring for the production caveat.
    if not job_intake_rate_limiter.check(str(payload.company_id)):
        LOGGER.warning(
            "Job intake rate limit exceeded",
            extra={
                "user_id": current_user.id,
                "actor_kind": actor_kind,
                "actor_id": str(actor.id),
                "company_id": str(payload.company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Job intake rate limit exceeded for this company",
            headers={"Retry-After": str(job_intake_rate_limiter.window_seconds)},
        )

    # 4. Build the workflow input ahead of any DB write so cross-field
    #    constraints (e.g. compensation_max >= compensation_min) raise a
    #    clean 422 BEFORE we insert an orphan Job row. The generated
    #    `JobIntakeRequest` schema does not enforce that relationship; the
    #    workflow input does, and we want both checks at the HTTP boundary.
    job_id = uuid4()
    workflow_id = f"job-intake-{job_id}"
    created_by = actor.id if actor_kind == "company_user" else None
    remote_onsite_value = (
        payload.remote_onsite.value if payload.remote_onsite is not None else None
    )

    try:
        workflow_input = JobIntakeInput(
            job_id=str(job_id),
            title=payload.title,
            jd_text=payload.jd_text,
            intake_notes=payload.intake_notes,
            remote_onsite=remote_onsite_value,
            location_text=payload.location_text,
            compensation_min=payload.compensation_min,
            compensation_max=payload.compensation_max,
            extra=payload.extra,
        )
    except ValidationError as exc:
        # Surface Pydantic errors as 422, mirroring FastAPI's body-validation
        # behaviour. We strip the `ctx` field from each error (it can carry
        # the originating exception object, which is not JSON-serializable)
        # and return only `loc` / `msg` / `type` — the same shape FastAPI's
        # default request-validation error handler emits.
        safe_errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        LOGGER.info(
            "Job intake validation failed",
            extra={
                "user_id": current_user.id,
                "actor_kind": actor_kind,
                "actor_id": str(actor.id),
                "company_id": str(payload.company_id),
                "errors": [{"loc": e["loc"], "type": e["type"]} for e in safe_errors],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=safe_errors,
        ) from exc

    # 5. INSERT the Job row. Classification fields are intentionally null —
    #    `classify_role_type` activity fills them inside the workflow.
    job_repo = JobRepository(session)
    try:
        await job_repo.create(
            id=job_id,
            company_id=payload.company_id,
            created_by=created_by,
            title=payload.title,
            jd_text=payload.jd_text,
            intake_notes=payload.intake_notes,
            remote_onsite=remote_onsite_value,
            location_text=payload.location_text,
            compensation_min=payload.compensation_min,
            compensation_max=payload.compensation_max,
            extra=payload.extra,
            status=JobStatus.INTAKE.value,
            workflow_id=workflow_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to insert Job row",
            extra={
                "user_id": current_user.id,
                "actor_kind": actor_kind,
                "actor_id": str(actor.id),
                "company_id": str(payload.company_id),
                "job_id": str(job_id),
                "workflow_id": workflow_id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create job",
        ) from exc

    # 6. Fire JobIntakeWorkflow (fire-and-forget). REJECT_DUPLICATE per D3
    #    — re-intake is a bug; reeval flows through HITL #2 in a separate PR.
    try:
        client = await get_temporal_client()
        await client.start_workflow(
            "JobIntakeWorkflow",
            workflow_input.model_dump(mode="json"),
            id=workflow_id,
            task_queue=_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Generic 5xx — never surface Temporal internals to the client.
        # Original error is logged for ops; the client sees a generic
        # message.
        LOGGER.exception(
            "Failed to start JobIntakeWorkflow",
            extra={
                "user_id": current_user.id,
                "actor_kind": actor_kind,
                "actor_id": str(actor.id),
                "company_id": str(payload.company_id),
                "job_id": str(job_id),
                "workflow_id": workflow_id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start job intake workflow",
        ) from exc

    # 6. Structured log for observability (no PII / no JD body).
    LOGGER.info(
        "Job intake accepted",
        extra={
            "user_id": current_user.id,
            "actor_kind": actor_kind,
            "actor_id": str(actor.id),
            "company_id": str(payload.company_id),
            "job_id": str(job_id),
            "workflow_id": workflow_id,
            "title_len": len(payload.title),
            "jd_text_len": len(payload.jd_text),
            "intake_notes_len": (
                len(payload.intake_notes) if payload.intake_notes is not None else 0
            ),
        },
    )

    response_data = JobIntakeAcceptedResponse(
        job_id=job_id,
        workflow_id=workflow_id,
        status=SpecJobStatus.intake,
    )
    return create_api_response(
        data=response_data.model_dump(mode="json"),
        message="Job intake accepted",
        request=request,
    )
