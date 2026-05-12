"""Operator-only endpoints for recruiter assignment HITL review.

GET  /api/v1/jobs/{job_id}/recruiter-assignment/proposal  - fetch current proposal
POST /api/v1/jobs/{job_id}/recruiter-assignment/approve   - send operator_approval signal
GET  /api/v1/jobs/{job_id}/recruiter-assignment/status    - proxy to workflow query handler

All three operations are gated by get_current_operator - non-operators
receive 403 before any DB or Temporal IO.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_operator
from app.core.database import get_async_session
from app.core.temporal_client import get_temporal_client
from app.database.models import Operator
from app.repositories.operator_proposals import OperatorProposalRepository
from app.schemas.product.recruiter_assignment import OperatorApprovalRequest
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/recruiter-assignment/proposal
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}/recruiter-assignment/proposal",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch the current recruiter assignment proposal for operator review",
    operation_id="get_recruiter_assignment_proposal",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an active Converio operator.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No outstanding (non-superseded) proposal exists for this job.",
        },
    },
)
async def get_recruiter_assignment_proposal(
    request: Request,
    job_id: UUID,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Return the latest non-superseded proposal for `job_id`.

    The repository filters on `superseded_at IS NULL` and orders by
    `created_at DESC`, so re-loops (operator rejected -> agent reproposed)
    naturally surface the freshest proposal. We expose the raw `payload`
    JSON the agent persisted plus the `quality_flag` so the operator UI
    can render the full review screen from a single round-trip.

    No PII / payload content is logged here - operator id and job id only.
    """
    repo = OperatorProposalRepository(session)
    proposal = await repo.fetch_latest_for_job(job_id)

    if proposal is None:
        LOGGER.info(
            "Proposal fetch: no outstanding proposal",
            extra={
                "operator_id": str(operator.id),
                "job_id": str(job_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No proposal found for this job",
        )

    LOGGER.info(
        "Proposal fetched for operator review",
        extra={
            "operator_id": str(operator.id),
            "job_id": str(job_id),
            "proposal_id": str(proposal.id),
            "workflow_id": proposal.workflow_id,
            "quality_flag": proposal.quality_flag,
        },
    )

    return create_api_response(
        data={
            "proposal_id": str(proposal.id),
            "job_id": str(proposal.job_id),
            "workflow_id": proposal.workflow_id,
            "quality_flag": proposal.quality_flag,
            "payload": proposal.payload,
            "created_at": proposal.created_at.isoformat(),
        },
        message="Proposal retrieved",
        request=request,
    )


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/recruiter-assignment/approve
# ---------------------------------------------------------------------------


@router.post(
    "/{job_id}/recruiter-assignment/approve",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send operator approval or rejection signal to RecruiterAssignmentWorkflow",
    operation_id="submit_recruiter_assignment_decision",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an active Converio operator.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No outstanding (non-superseded) proposal exists for this job.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Decision payload failed schema validation.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Failed to dispatch the workflow signal to Temporal.",
        },
    },
)
async def submit_recruiter_assignment_decision(
    request: Request,
    job_id: UUID,
    payload: OperatorApprovalRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Forward the operator's approve / reject decision to the workflow.

    The decision is sent as an `operator_approval` Temporal signal to the
    workflow id stored on the latest proposal row. We enrich the payload
    with `operator_id` server-side so the workflow's audit trail records
    *which* operator made the decision - clients cannot spoof this.

    Security rules (CLAUDE.md compliance):
      - `confirmed_recruiter_ids`, `override_set`, and `notes` are NEVER
        logged. They may contain operator commentary, candidate IDs, or
        rejection reasoning that should not leak into log aggregators.
      - On Temporal failure we surface a generic 502; the underlying
        exception is logged for ops but not echoed to the client.

    Idempotency: the workflow's signal handler is responsible for
    deduplicating repeated approvals; this endpoint is a thin proxy.
    """
    repo = OperatorProposalRepository(session)
    proposal = await repo.fetch_latest_for_job(job_id)

    if proposal is None:
        LOGGER.info(
            "Approval signal: no outstanding proposal",
            extra={
                "operator_id": str(operator.id),
                "job_id": str(job_id),
                "decision": payload.decision,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No proposal found for this job",
        )

    # Enrich the signal with operator id sourced from the auth context (NOT
    # the request body) so the workflow has a trustworthy actor record.
    signal_dict = payload.model_dump() | {"operator_id": str(operator.id)}

    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(proposal.workflow_id)
        await handle.signal("operator_approval", signal_dict)
    except HTTPException:
        raise
    except Exception as exc:
        # Generic 502 to the client - never expose Temporal internals.
        LOGGER.exception(
            "Failed to dispatch operator_approval signal",
            extra={
                "operator_id": str(operator.id),
                "job_id": str(job_id),
                "workflow_id": proposal.workflow_id,
                "decision": payload.decision,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to dispatch workflow signal. Try again.",
        ) from exc

    # Audit log: decision + operator id only. NEVER log the payload body
    # (confirmed_recruiter_ids / override_set / notes are sensitive).
    LOGGER.info(
        "Operator approval signal sent",
        extra={
            "job_id": str(job_id),
            "decision": payload.decision,
            "operator_id": str(operator.id),
            "workflow_id": proposal.workflow_id,
            "proposal_id": str(proposal.id),
        },
    )

    return create_api_response(
        data={
            "job_id": str(job_id),
            "decision": payload.decision,
            "workflow_id": proposal.workflow_id,
        },
        message=f"Signal '{payload.decision}' sent to workflow",
        request=request,
    )


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/recruiter-assignment/status
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}/recruiter-assignment/status",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="Get recruiter assignment workflow status",
    operation_id="get_recruiter_assignment_status",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an active Converio operator.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No proposal (and therefore no known workflow) exists for this job.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Failed to query the Temporal workflow.",
        },
    },
)
async def get_recruiter_assignment_status(
    request: Request,
    job_id: UUID,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Proxy `current_phase` query to the RecruiterAssignmentWorkflow.

    The proposal row carries the canonical `workflow_id` for the running
    workflow; we use it to construct a workflow handle and run the
    `current_phase` query. Returns the phase string (e.g. `searching`,
    `awaiting_operator`, `assigning`, `notifying`, `completed`) so the
    operator console can render a live progress indicator.

    On Temporal query failure we return 502 with a generic message;
    the original exception is logged for ops triage.
    """
    repo = OperatorProposalRepository(session)
    proposal = await repo.fetch_latest_for_job(job_id)

    if proposal is None:
        LOGGER.info(
            "Status query: no outstanding proposal",
            extra={
                "operator_id": str(operator.id),
                "job_id": str(job_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No proposal found for this job",
        )

    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(proposal.workflow_id)
        phase = await handle.query("current_phase")
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to query workflow current_phase",
            extra={
                "operator_id": str(operator.id),
                "job_id": str(job_id),
                "workflow_id": proposal.workflow_id,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to query workflow status. Try again.",
        ) from exc

    LOGGER.info(
        "Workflow status queried",
        extra={
            "operator_id": str(operator.id),
            "job_id": str(job_id),
            "workflow_id": proposal.workflow_id,
            "phase": phase,
        },
    )

    return create_api_response(
        data={
            "job_id": str(job_id),
            "workflow_id": proposal.workflow_id,
            "phase": phase,
        },
        message="Workflow status retrieved",
        request=request,
    )
