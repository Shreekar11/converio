"""Candidate-facing API endpoints (recruiter portal).

F1: POST /api/v1/candidates/index — recruiter-authenticated resume upload.
Triggers CandidateIndexingWorkflow on the Temporal `converio-queue`.
"""
from __future__ import annotations

import base64
import mimetypes
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.temporal_client import get_temporal_client
from app.database.models import Recruiter
from app.schemas.product.candidate import CandidateIndexingInput
from app.temporal.product.candidate_indexing.workflows.candidate_indexing_workflow import (
    CandidateIndexingWorkflow,
)
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)

# Allowlist (not denylist) of accepted resume MIME types.
_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "text/markdown",
    }
)

# 5 MiB hard cap on resume uploads.
_MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024

# Temporal task queue shared by all product workflows.
_TASK_QUEUE: str = "converio-queue"


@router.post(
    "/index",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload candidate resume and trigger indexing workflow",
    operation_id="index_candidate",
)
async def index_candidate(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
    file: Annotated[
        UploadFile,
        File(description="Resume file (PDF, DOCX, or plain text)"),
    ],
    notes: Annotated[str | None, Form()] = None,
) -> ApiResponse:
    """Accept a recruiter-uploaded resume and fire CandidateIndexingWorkflow.

    Returns 202 Accepted with the workflow_id so the client can poll status.
    Authentication is enforced via `get_current_user`. The authenticated user
    is resolved to a `Recruiter` row via `supabase_user_id`; if no row exists
    the workflow still fires with `source_recruiter_id=None` (e.g. admin user
    seeding from the recruiter portal during onboarding).
    """
    # 1. MIME allowlist enforcement (415 on miss).
    mime_type = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    )
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported file type. Allowed: PDF, DOCX, plain text, markdown.",
        )

    # 2. Size limit enforcement (413 on oversize).
    raw_bytes = await file.read()
    if len(raw_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds 5MB limit.",
        )
    if len(raw_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    raw_bytes_b64 = base64.b64encode(raw_bytes).decode("ascii")

    # 3. Resolve recruiter row from authenticated Supabase user (parameterized query).
    source_recruiter_id: str | None = None
    try:
        result = await session.execute(
            select(Recruiter).where(Recruiter.supabase_user_id == current_user.id)
        )
        recruiter = result.scalar_one_or_none()
        if recruiter is not None:
            source_recruiter_id = str(recruiter.id)
    except Exception as exc:
        # Do not surface internals to the client; log structured warning and continue.
        LOGGER.warning(
            "Could not resolve recruiter from authenticated user",
            extra={"user_id": current_user.id, "error": str(exc)},
        )

    # 4. Fire CandidateIndexingWorkflow.
    workflow_id = f"candidate-indexing-{uuid4()}"
    client = await get_temporal_client()

    workflow_input = CandidateIndexingInput(
        raw_bytes_b64=raw_bytes_b64,
        mime_type=mime_type,
        source="recruiter_upload",
        source_recruiter_id=source_recruiter_id,
    )

    await client.start_workflow(
        CandidateIndexingWorkflow.run,
        workflow_input.model_dump(mode="json"),
        id=workflow_id,
        task_queue=_TASK_QUEUE,
    )

    LOGGER.info(
        "Candidate indexing workflow started",
        extra={
            "workflow_id": workflow_id,
            "filename": file.filename,
            "mime_type": mime_type,
            "size_bytes": len(raw_bytes),
            "user_id": current_user.id,
            "recruiter_id": source_recruiter_id,
        },
    )

    return create_api_response(
        data={"workflow_id": workflow_id, "filename": file.filename},
        message="Resume uploaded — indexing started",
        request=request,
    )
