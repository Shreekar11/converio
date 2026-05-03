"""Self-serve authentication endpoints — company signup, recruiter signup, identity resolver.

GET   /api/v1/auth/me               — resolve authenticated user role and profile
POST  /api/v1/auth/company/signup   — self-serve company registration
POST  /api/v1/auth/recruiter/signup — self-serve recruiter registration

All three endpoints require a valid Supabase JWT (Bearer token). The role returned
by /auth/me is the single source of truth for role routing in the frontend.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.schemas.generated.auth import (
    CompanySignupRequest,
    RecruiterSignupRequest,
)
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# GET /auth/me — identity resolver
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="Resolve authenticated user role and profile",
    operation_id="get_auth_me",
)
async def get_auth_me(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Return the role and role-specific profile for the authenticated user."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented",
    )


# ---------------------------------------------------------------------------
# POST /auth/company/signup — self-serve company registration
# ---------------------------------------------------------------------------


@router.post(
    "/company/signup",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Self-serve company registration",
    operation_id="company_signup",
)
async def company_signup(
    request: Request,
    payload: CompanySignupRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Create a self-serve company tenant linked to the authenticated user."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented",
    )


# ---------------------------------------------------------------------------
# POST /auth/recruiter/signup — self-serve recruiter registration
# ---------------------------------------------------------------------------


@router.post(
    "/recruiter/signup",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Self-serve recruiter registration",
    operation_id="recruiter_signup",
)
async def recruiter_signup(
    request: Request,
    payload: RecruiterSignupRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Create a self-serve recruiter profile linked to the authenticated user."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented",
    )
