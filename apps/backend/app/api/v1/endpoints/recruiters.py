"""Recruiter-facing API endpoints (operator + recruiter portal).

POST /api/v1/recruiters/{recruiter_id}/index — fires RecruiterIndexingWorkflow
on the Temporal `converio-queue`. Loads the recruiter row + linked clients +
linked placements from PG, builds a `RecruiterProfile`, and starts the
workflow with `source="onboarding"`.

This endpoint does NOT perform any indexing logic itself — it only loads
data and dispatches the workflow. Re-indexing after `Add Client` / `Add
Placement` mutations is supported via `ALLOW_DUPLICATE` reuse policy.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDReusePolicy

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.temporal_client import get_temporal_client
from app.repositories.recruiter_clients import RecruiterClientRepository
from app.repositories.recruiter_placements import RecruiterPlacementRepository
from app.repositories.recruiters import RecruiterRepository
from app.schemas.enums import (
    CompanyStage,
    RecruitedFundingStage,
    RoleCategory,
    WorkspaceType,
)
from app.schemas.product.recruiter import (
    RecruiterClientItem,
    RecruiterIndexingInput,
    RecruiterPlacementItem,
    RecruiterProfile,
)
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)

# Temporal task queue shared by all product workflows.
_TASK_QUEUE: str = "converio-queue"


def _safe_role_categories(values: list[str] | None) -> list[RoleCategory]:
    """Cast string list to RoleCategory enum, skipping unknown values.

    Defensive: seed/test data or a future schema drift could yield a string
    that no longer maps to the enum. We log + skip rather than fail the
    whole request so partial profiles can still be indexed.
    """
    if not values:
        return []
    out: list[RoleCategory] = []
    for v in values:
        try:
            out.append(RoleCategory(v))
        except ValueError:
            LOGGER.warning(
                "Skipping unknown domain_expertise value",
                extra={"value": v},
            )
    return out


def _safe_enum(enum_cls, value):
    """Cast a string to the given enum class; return None on miss (logged)."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        LOGGER.warning(
            "Skipping unknown enum value",
            extra={"enum": enum_cls.__name__, "value": value},
        )
        return None


@router.post(
    "/{recruiter_id}/index",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger recruiter indexing workflow",
    operation_id="index_recruiter",
)
async def index_recruiter(
    request: Request,
    recruiter_id: UUID,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Build RecruiterProfile from PG and fire RecruiterIndexingWorkflow.

    Returns 202 Accepted with the `workflow_id` so the client can poll status
    via the Temporal `get_status` query handler.

    Auth: any authenticated user (mirrors candidates.py for now). Tenant /
    role-based authorization will tighten when the recruiter portal lands.
    """
    # 1. Load recruiter row (404 on miss).
    recruiter_repo = RecruiterRepository(session)
    recruiter = await recruiter_repo.get_by_id(recruiter_id)
    if recruiter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recruiter not found",
        )

    # 2. Load linked clients + placements.
    clients = await RecruiterClientRepository(session).get_by_recruiter(recruiter_id)
    placements = await RecruiterPlacementRepository(session).get_by_recruiter(recruiter_id)

    # 3. Build RecruiterProfile — defensively cast string fields to enums so
    #    a value drift in PG (older row, manual edit) does not 500 the request.
    profile = RecruiterProfile(
        recruiter_id=str(recruiter.id),
        full_name=recruiter.full_name,
        email=recruiter.email,
        linkedin_url=recruiter.linkedin_url,
        bio=recruiter.bio,
        domain_expertise=_safe_role_categories(recruiter.domain_expertise),
        workspace_type=_safe_enum(WorkspaceType, recruiter.workspace_type),
        recruited_funding_stage=_safe_enum(
            RecruitedFundingStage, recruiter.recruited_funding_stage
        ),
        past_clients=[
            RecruiterClientItem(
                client_company_name=c.client_company_name,
                description=c.description,
                role_focus=list(c.role_focus or []),
            )
            for c in clients
        ],
        past_placements=[
            RecruiterPlacementItem(
                candidate_name=p.candidate_name,
                company_name=p.company_name,
                company_stage=_safe_enum(CompanyStage, p.company_stage),
                role_title=p.role_title,
                placed_at=p.placed_at.isoformat() if p.placed_at is not None else None,
                description=p.description,
            )
            for p in placements
        ],
    )

    # 4. Fire RecruiterIndexingWorkflow.
    workflow_id = f"recruiter-indexing-{recruiter_id}"
    inp = RecruiterIndexingInput(
        input_kind="profile",
        profile=profile,
        source="onboarding",
    )

    try:
        client = await get_temporal_client()
        await client.start_workflow(
            "RecruiterIndexingWorkflow",
            inp.model_dump(mode="json"),
            id=workflow_id,
            task_queue=_TASK_QUEUE,
            # ALLOW_DUPLICATE so re-indexing after `Add Client` / `Add Placement`
            # mutations works without operator intervention. candidates.py uses
            # a fresh uuid per upload so it does not need to override; recruiter
            # ids are stable per recruiter, so duplicate reuse is required.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Do not surface internals (host, namespace, stack) to the client.
        LOGGER.exception(
            "Failed to start RecruiterIndexingWorkflow",
            extra={
                "workflow_id": workflow_id,
                "recruiter_id": str(recruiter_id),
                "user_id": current_user.id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start recruiter indexing workflow",
        ) from exc

    LOGGER.info(
        "Recruiter indexing workflow started",
        extra={
            "workflow_id": workflow_id,
            "recruiter_id": str(recruiter_id),
            "user_id": current_user.id,
            "source": "onboarding",
        },
    )

    return create_api_response(
        data={
            "workflow_id": workflow_id,
            "recruiter_id": str(recruiter_id),
        },
        message="Recruiter indexing started",
        request=request,
    )
