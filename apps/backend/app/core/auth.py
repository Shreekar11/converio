from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.jwt import jwt_verifier
from app.database.models import CompanyUser, Operator, Recruiter
from app.repositories.companies import CompanyRepository
from app.repositories.company_users import CompanyUserRepository
from app.repositories.operators import OperatorRepository
from app.repositories.recruiters import RecruiterRepository
from app.schemas.enums import CompanyStatus, OperatorStatus, RecruiterStatus
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)
security = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    id: str
    email: str | None = None
    role: str = "user"
    app_metadata: dict = {}
    user_metadata: dict = {}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> CurrentUser:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = await jwt_verifier.verify_token(credentials.credentials)
        return CurrentUser(
            id=claims.sub,
            email=claims.email,
            role=claims.role or "user",
            app_metadata=claims.app_metadata or {},
            user_metadata=claims.user_metadata or {},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_current_operator(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> Operator:
    """Resolve authenticated user to an active Operator row.

    Raises:
        HTTPException 403 if no operator row is linked to this user, or if
            the operator's status is not 'active'.
    """
    operator = await OperatorRepository(session).get_by_supabase_id(
        current_user.id
    )

    # Collapse "no row" and "inactive" into a single 403 to avoid leaking the
    # distinction (operator presence is itself sensitive: an attacker probing
    # which Supabase users are Converio operators must not be able to tell
    # "no operator row" from "operator suspended").
    if operator is None:
        LOGGER.warning(
            "Operator auth check failed",
            extra={"user_id": current_user.id, "reason": "no_operator_row"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required",
        )

    if operator.status != OperatorStatus.ACTIVE.value:
        LOGGER.warning(
            "Operator auth check failed",
            extra={
                "user_id": current_user.id,
                "operator_id": str(operator.id),
                "reason": "inactive_operator",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required",
        )

    return operator


async def get_current_company_user(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> CompanyUser:
    """Resolve authenticated user to a CompanyUser seat row.

    Used by company-facing endpoints (hiring-manager / admin seats). Returns
    the seated row regardless of the linked company's lifecycle status —
    callers that need a company in `active` state should depend on
    `get_current_active_company_user` instead.

    Raises:
        HTTPException 403 if no company-user row is linked to this Supabase
            auth user. The detail is intentionally generic so it cannot be
            used to enumerate which auth users are seated company users
            versus operators or recruiters.
    """
    company_user = await CompanyUserRepository(session).get_by_supabase_user_id(
        current_user.id
    )

    if company_user is None:
        LOGGER.warning(
            "Company user auth check failed",
            extra={
                "user_id": current_user.id,
                "reason": "no_company_user_row",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a company user",
        )

    return company_user


async def get_current_recruiter(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> Recruiter:
    """Resolve authenticated user to a non-suspended Recruiter row.

    Returns recruiters in `pending` (mid-onboarding wizard) or `active`
    states. `suspended` recruiters are blocked here so downstream endpoints
    don't need to re-check.

    Raises:
        HTTPException 403 if no recruiter row is linked to this user, or if
            the recruiter's status is `suspended`. The two cases use distinct
            detail messages because recruiter onboarding is self-serve and
            the FE needs to distinguish "you have no recruiter profile yet"
            from "your account was suspended" to surface the right CTA.
    """
    recruiter = await RecruiterRepository(session).get_by_supabase_id(
        current_user.id
    )

    if recruiter is None:
        LOGGER.warning(
            "Recruiter auth check failed",
            extra={
                "user_id": current_user.id,
                "reason": "no_recruiter_row",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a recruiter",
        )

    if recruiter.status == RecruiterStatus.SUSPENDED.value:
        LOGGER.warning(
            "Recruiter auth check failed",
            extra={
                "user_id": current_user.id,
                "recruiter_id": str(recruiter.id),
                "reason": "suspended_recruiter",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Recruiter account suspended",
        )

    return recruiter


async def get_current_active_company_user(
    company_user: Annotated[CompanyUser, Depends(get_current_company_user)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> CompanyUser:
    """Resolve to a CompanyUser whose company is in `active` status.

    Layered on top of `get_current_company_user`: same identity check, plus
    a guard that the linked company is past operator review and not paused
    or churned.

    Note: we deliberately re-fetch the company by id here rather than relying
    on `company_user.company` lazy-load. The ORM relationship may reflect a
    stale snapshot bound to a different session, and we want the freshest
    `status` value from the DB on every request — operators flipping a
    company between `active` / `paused` must take effect immediately for
    in-flight sessions.

    Raises:
        HTTPException 403 if the linked company row is missing (race with
            company deletion) or its status is anything other than `active`.
    """
    company = await CompanyRepository(session).get_by_id(company_user.company_id)

    if company is None:
        LOGGER.warning(
            "Active company user auth check failed",
            extra={
                "company_user_id": str(company_user.id),
                "company_id": str(company_user.company_id),
                "reason": "company_missing",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Company not found",
        )

    if company.status != CompanyStatus.ACTIVE.value:
        LOGGER.warning(
            "Active company user auth check failed",
            extra={
                "company_user_id": str(company_user.id),
                "company_id": str(company.id),
                "company_status": company.status,
                "reason": "company_not_active",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Company not active",
        )

    return company_user
