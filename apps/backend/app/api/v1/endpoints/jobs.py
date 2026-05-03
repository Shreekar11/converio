"""Job intake API endpoint (Phase E1).

POST /api/v1/jobs/intake — managed job intake submission. Authenticates
the caller as an active `CompanyUser` (seat row at a company whose
`status == "active"`), INSERTs a `Job` row with `status="intake"` scoped
to `company_user.company_id`, and fires `JobIntakeWorkflow`
fire-and-forget on the Temporal `converio-queue` with
`WorkflowIDReusePolicy.REJECT_DUPLICATE` (D3).

Tenant scoping rule: `company_id` is sourced from the authenticated
`CompanyUser` row, NEVER from the request body. The body schema retains
a `company_id` field for backward compatibility with the published
OpenAPI spec, but the value is ignored — derived auth-context wins.

Request / response shapes are imported from `app.schemas.generated.jobs`
(codegen output of `app/api/v1/specs/jobs.json`); the spec is the source
of truth.
"""
from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDReusePolicy

from app.core.auth import get_current_active_company_user
from app.core.database import get_async_session
from app.core.rate_limit import job_intake_rate_limiter
from app.core.temporal_client import get_temporal_client
from app.database.models import CompanyUser
from app.repositories.jobs import JobRepository
from app.schemas.enums import JobStatus
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


@router.post(
    "/intake",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a managed job intake and fire JobIntakeWorkflow",
    operation_id="submit_job_intake",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Caller is not a seated company user, or the linked company "
                "is not active."
            )
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Rate limit exceeded for this company (10/hour).",
        },
    },
)
async def submit_job_intake(
    request: Request,
    payload: JobIntakeRequest,
    company_user: Annotated[CompanyUser, Depends(get_current_active_company_user)],
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

    Auth: requires a `CompanyUser` row whose linked `Company.status` is
    `active`. `pending_review` / `paused` / `churned` companies cannot
    submit intakes — operators must promote the company to `active`
    first. The dep `get_current_active_company_user` returns 403 with
    `detail="Company not active"` (or `"Not a company user"` when the
    Supabase user has no seat at all).

    Tenant scoping: `company_id` is taken from `company_user.company_id`,
    NOT from the request body. The body still carries a `company_id`
    field for OpenAPI-spec compatibility; its value is ignored to prevent
    a seated user from submitting intakes against an arbitrary company.

    Logging: `jd_text` and `intake_notes` are PII / company-confidential
    and are NEVER written to logs. Only the lengths are logged for
    operational signal.
    """
    # 1. Tenant id is sourced from auth context, not the request body.
    company_id = company_user.company_id

    # 2. Rate limit (10/hour/company_id). Single-process; see
    #    `app.core.rate_limit` docstring for the production caveat.
    if not job_intake_rate_limiter.check(str(company_id)):
        LOGGER.warning(
            "Job intake rate limit exceeded",
            extra={
                "company_user_id": str(company_user.id),
                "company_id": str(company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Job intake rate limit exceeded for this company",
            headers={"Retry-After": str(job_intake_rate_limiter.window_seconds)},
        )

    # 3. Build the workflow input ahead of any DB write so cross-field
    #    constraints (e.g. compensation_max >= compensation_min) raise a
    #    clean 422 BEFORE we insert an orphan Job row. The generated
    #    `JobIntakeRequest` schema does not enforce that relationship; the
    #    workflow input does, and we want both checks at the HTTP boundary.
    job_id = uuid4()
    workflow_id = f"job-intake-{job_id}"
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
                "company_user_id": str(company_user.id),
                "company_id": str(company_id),
                "errors": [{"loc": e["loc"], "type": e["type"]} for e in safe_errors],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=safe_errors,
        ) from exc

    # 4. INSERT the Job row. Classification fields are intentionally null —
    #    `classify_role_type` activity fills them inside the workflow.
    job_repo = JobRepository(session)
    try:
        await job_repo.create(
            id=job_id,
            company_id=company_id,
            created_by=company_user.id,
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
                "company_user_id": str(company_user.id),
                "company_id": str(company_id),
                "job_id": str(job_id),
                "workflow_id": workflow_id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create job",
        ) from exc

    # 5. Fire JobIntakeWorkflow (fire-and-forget). REJECT_DUPLICATE per D3
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
                "company_user_id": str(company_user.id),
                "company_id": str(company_id),
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
            "company_user_id": str(company_user.id),
            "company_id": str(company_id),
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
