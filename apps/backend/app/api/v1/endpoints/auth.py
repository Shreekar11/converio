"""Self-serve authentication endpoints — company signup, recruiter signup, identity resolver.

GET   /api/v1/auth/me               — resolve authenticated user role and profile
POST  /api/v1/auth/company/signup   — self-serve company registration
POST  /api/v1/auth/recruiter/signup — self-serve recruiter registration

All three endpoints require a valid Supabase JWT (Bearer token). The role returned
by /auth/me is the single source of truth for role routing in the frontend.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_async_session
from app.core.rate_limit import signup_rate_limiter
from app.database.models import Company, CompanyUser, Operator, Recruiter
from app.repositories.companies import CompanyRepository
from app.repositories.company_users import CompanyUserRepository
from app.repositories.operators import OperatorRepository
from app.repositories.recruiters import RecruiterRepository
from app.schemas.enums import CompanyStatus, CompanyUserRole
from app.schemas.generated.auth import (
    CompanySignupRequest,
    RecruiterSignupRequest,
)
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# Projection helpers (ORM row -> JSON-friendly dict matching AuthMeResponse)
# ---------------------------------------------------------------------------
#
# /auth/me returns a polymorphic `profile` shape — the response model declares
# it as `dict[str, Any] | None` so each role projects to its own dict layout.
# Datetimes/UUIDs are coerced to strings so the response envelope can be
# JSON-serialized without a Pydantic round-trip on the calling endpoint.


def _project_operator(op: Operator) -> dict[str, Any]:
    """Map an `Operator` ORM row to the `/auth/me` operator profile dict."""
    return {
        "id": str(op.id),
        "email": op.email,
        "full_name": op.full_name,
        "status": op.status,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
    }


def _project_company_user_profile(
    user: CompanyUser, company: Company
) -> dict[str, Any]:
    """Map a `CompanyUser` + linked `Company` to the company-user profile dict.

    The company-user role projection nests the company under `profile.company`
    so the frontend can render company branding without a second round-trip
    to `/companies/{id}`.
    """
    return {
        "user": {
            "id": str(user.id),
            "company_id": str(user.company_id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        },
        "company": {
            "id": str(company.id),
            "name": company.name,
            "stage": company.stage,
            "industry": company.industry,
            "website": company.website,
            "logo_url": company.logo_url,
            "company_size_range": company.company_size_range,
            "founding_year": company.founding_year,
            "hq_location": company.hq_location,
            "description": company.description,
            "status": company.status,
            "created_at": company.created_at.isoformat()
            if company.created_at
            else None,
            "updated_at": company.updated_at.isoformat()
            if company.updated_at
            else None,
        },
    }


def _project_recruiter(r: Recruiter) -> dict[str, Any]:
    """Map a `Recruiter` ORM row to the `/auth/me` recruiter profile dict."""
    return {
        "id": str(r.id),
        "email": r.email,
        "full_name": r.full_name,
        "status": r.status,
        "domain_expertise": list(r.domain_expertise or []),
        "workspace_type": r.workspace_type,
        "recruited_funding_stage": r.recruited_funding_stage,
        "at_capacity": bool(r.at_capacity),
        "total_placements": int(r.total_placements or 0),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


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
    """Return the role and role-specific profile for the authenticated user.

    Lookup precedence (short-circuits on the first match):

    1. Operator — internal Converio talent-ops; highest precedence so an op
       who also seats themselves on a test company never resolves as a
       company_user.
    2. CompanyUser linked by `supabase_user_id` — already-onboarded seat.
    3. Recruiter linked by `supabase_user_id` — already-onboarded recruiter.
    4. Email backfill — pre-provisioned company seat (created by an operator
       with `supabase_user_id=null`) gets linked on first sign-in. Only runs
       when steps 1-3 miss AND the JWT carries a verified email claim.
    5. Unregistered — JWT is valid but no role row exists. The frontend uses
       this to route into the self-serve company/recruiter signup wizard.

    Never raises on identity lookup miss — `unregistered` is a valid terminal
    state. The caller is already authenticated (JWT verified upstream); we're
    only resolving role + profile.
    """
    sub = current_user.id

    # --- 1. Operator ---------------------------------------------------------
    operator = await OperatorRepository(session).get_by_supabase_id(sub)
    if operator is not None:
        LOGGER.info(
            "Auth me resolved",
            extra={
                "user_id": sub,
                "role": "operator",
                "operator_id": str(operator.id),
            },
        )
        return create_api_response(
            data={
                "role": "operator",
                "profile": _project_operator(operator),
                "onboarding_state": None,
            },
            message="User resolved",
            request=request,
        )

    # --- 2. CompanyUser by supabase_user_id ----------------------------------
    company_user_repo = CompanyUserRepository(session)
    company_user = await company_user_repo.get_by_supabase_user_id(sub)
    if company_user is not None:
        company = await CompanyRepository(session).get_by_id(company_user.company_id)
        if company is None:
            # Defensive: the company was deleted out from under a seat. Treat
            # as unregistered rather than 500 — the operator console will
            # surface the orphaned seat separately.
            LOGGER.error(
                "Company user references missing company",
                extra={
                    "user_id": sub,
                    "company_user_id": str(company_user.id),
                    "company_id": str(company_user.company_id),
                },
            )
            return create_api_response(
                data={
                    "role": "unregistered",
                    "profile": None,
                    "onboarding_state": None,
                },
                message="User resolved",
                request=request,
            )

        LOGGER.info(
            "Auth me resolved",
            extra={
                "user_id": sub,
                "role": "company_user",
                "company_user_id": str(company_user.id),
                "company_id": str(company.id),
                "company_status": company.status,
            },
        )
        return create_api_response(
            data={
                "role": "company_user",
                "profile": _project_company_user_profile(company_user, company),
                "onboarding_state": {"company_status": company.status},
            },
            message="User resolved",
            request=request,
        )

    # --- 3. Recruiter by supabase_user_id ------------------------------------
    recruiter = await RecruiterRepository(session).get_by_supabase_id(sub)
    if recruiter is not None:
        LOGGER.info(
            "Auth me resolved",
            extra={
                "user_id": sub,
                "role": "recruiter",
                "recruiter_id": str(recruiter.id),
                "recruiter_status": recruiter.status,
            },
        )
        return create_api_response(
            data={
                "role": "recruiter",
                "profile": _project_recruiter(recruiter),
                "onboarding_state": {"recruiter_status": recruiter.status},
            },
            message="User resolved",
            request=request,
        )

    # --- 4. Email-backfill for pre-provisioned company seats -----------------
    # This branch only runs after every supabase_user_id lookup has missed.
    # Operator pre-provisioned seats are inserted with `supabase_user_id=null`
    # and get linked here on the user's first sign-in. The Supabase JWT email
    # claim is the join key — we never trust client-supplied email.
    if current_user.email is not None:
        seated_user = await company_user_repo.get_by_email(current_user.email)
        if seated_user is not None and seated_user.supabase_user_id is None:
            linked = await company_user_repo.link_supabase_user_id(
                seated_user.id, sub
            )
            if linked is not None:
                company = await CompanyRepository(session).get_by_id(
                    linked.company_id
                )
                # Structured log only — do NOT log raw email; we identify by
                # ids the operator console can resolve.
                LOGGER.info(
                    "seat_backfilled",
                    extra={
                        "event": "seat_backfilled",
                        "user_id": sub,
                        "company_user_id": str(linked.id),
                        "company_id": str(linked.company_id),
                    },
                )
                if company is not None:
                    return create_api_response(
                        data={
                            "role": "company_user",
                            "profile": _project_company_user_profile(
                                linked, company
                            ),
                            "onboarding_state": {
                                "company_status": company.status
                            },
                        },
                        message="User resolved",
                        request=request,
                    )
                # Linked seat but missing company — log and fall through to
                # unregistered so the FE can re-route the user gracefully.
                LOGGER.error(
                    "Backfilled seat references missing company",
                    extra={
                        "user_id": sub,
                        "company_user_id": str(linked.id),
                        "company_id": str(linked.company_id),
                    },
                )

    # --- 5. Unregistered -----------------------------------------------------
    LOGGER.info(
        "Auth me resolved",
        extra={"user_id": sub, "role": "unregistered"},
    )
    return create_api_response(
        data={
            "role": "unregistered",
            "profile": None,
            "onboarding_state": None,
        },
        message="User resolved",
        request=request,
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
    """Create a self-serve company tenant linked to the authenticated user.

    Identity is taken exclusively from the verified Supabase JWT — never from
    the request body. Six logic branches:

    1. Defensive email-claim check (the JWT may carry no email — anonymous /
       phone-only sign-in shouldn't be able to claim a company seat).
    2. Per-`sub` rate limit guard against retry storms / abuse.
    3. Cross-role email uniqueness — a single email can be at most one of
       operator, recruiter, or company-user across the platform.
    4. Already-onboarded short-circuit — re-running signup after success
       returns 409 rather than creating a second tenant.
    5. Pre-provisioned seat link path — if an operator pre-seated this email,
       backfill the `supabase_user_id` and return the seat instead of
       creating a new company.
    6. Standard self-serve path — duplicate-name guard, create company in
       `pending_review`, create admin company-user, return 201.
    """
    # --- 1. Email claim must be present ------------------------------------
    if current_user.email is None:
        LOGGER.warning(
            "Company signup rejected: missing email claim",
            extra={"user_id": current_user.id},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email claim required for self-serve signup",
        )

    email = current_user.email

    # --- 2. Rate limit (keyed by Supabase sub) -----------------------------
    if not signup_rate_limiter.check(current_user.id):
        LOGGER.warning(
            "Company signup rate-limited",
            extra={"user_id": current_user.id},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signup attempts; try again later",
            headers={"Retry-After": str(signup_rate_limiter.window_seconds)},
        )

    # --- 3. Cross-role email uniqueness ------------------------------------
    operator_match = await OperatorRepository(session).get_by_email(email)
    if operator_match is not None:
        LOGGER.warning(
            "Company signup blocked: email is operator",
            extra={
                "user_id": current_user.id,
                "operator_id": str(operator_match.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
            headers={"X-Error-Code": "email_in_use_operator"},
        )

    recruiter_match = await RecruiterRepository(session).get_by_email(email)
    if recruiter_match is not None:
        LOGGER.warning(
            "Company signup blocked: email is recruiter",
            extra={
                "user_id": current_user.id,
                "recruiter_id": str(recruiter_match.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
            headers={"X-Error-Code": "email_in_use_recruiter"},
        )

    # --- 4. Already-onboarded short-circuit --------------------------------
    company_user_repo = CompanyUserRepository(session)
    already_onboarded = await company_user_repo.get_by_supabase_user_id(
        current_user.id
    )
    if already_onboarded is not None:
        LOGGER.warning(
            "Company signup blocked: already onboarded",
            extra={
                "user_id": current_user.id,
                "company_user_id": str(already_onboarded.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already onboarded as company user",
            headers={"X-Error-Code": "already_onboarded"},
        )

    # --- 5. Pre-provisioned seat link path ---------------------------------
    company_repo = CompanyRepository(session)
    existing_seat = await company_user_repo.get_by_email(email)
    if existing_seat is not None and existing_seat.supabase_user_id is None:
        linked = await company_user_repo.link_supabase_user_id(
            existing_seat.id, current_user.id
        )
        # Treat the link helper's return as the post-commit row; fall through
        # to defensive checks if it somehow returned None (row deleted
        # mid-flight).
        seat = linked if linked is not None else existing_seat
        company = await company_repo.get_by_id(seat.company_id)
        if company is None:
            LOGGER.error(
                "Pre-provisioned seat references missing company",
                extra={
                    "user_id": current_user.id,
                    "company_user_id": str(seat.id),
                    "company_id": str(seat.company_id),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to complete signup",
            )
        LOGGER.info(
            "Pre-provisioned seat linked",
            extra={
                "user_id": current_user.id,
                "company_user_id": str(seat.id),
                "company_id": str(seat.company_id),
            },
        )
        return create_api_response(
            data={
                "role": "company_user",
                "profile": _project_company_user_profile(seat, company),
                "onboarding_state": {"company_status": company.status},
            },
            message="Seat linked",
            request=request,
        )

    # --- 6. Standard self-serve path ---------------------------------------
    duplicate = await company_repo.get_by_name_ci(payload.name)
    if duplicate is not None:
        LOGGER.warning(
            "Company signup blocked: duplicate name",
            extra={
                "user_id": current_user.id,
                "existing_company_id": str(duplicate.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company name already exists",
        )

    try:
        company = await company_repo.create(
            name=payload.name,
            stage=payload.stage.value if payload.stage is not None else None,
            industry=payload.industry,
            website=str(payload.website) if payload.website is not None else None,
            logo_url=str(payload.logo_url) if payload.logo_url is not None else None,
            company_size_range=(
                payload.company_size_range.value
                if payload.company_size_range is not None
                else None
            ),
            founding_year=payload.founding_year,
            hq_location=payload.hq_location,
            description=payload.description,
            status=CompanyStatus.PENDING_REVIEW.value,
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to create company during self-serve signup",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete signup",
        ) from exc

    full_name = current_user.user_metadata.get("full_name", "") or ""

    try:
        # TODO: wrap company + company_user creation in a single transaction
        # so a failure here doesn't leave an orphan company. Today the
        # repository commits after each insert.
        company_user = await company_user_repo.create(
            company_id=company.id,
            email=email,
            full_name=full_name,
            role=CompanyUserRole.ADMIN.value,
            supabase_user_id=current_user.id,
        )
    except Exception as exc:
        LOGGER.error(
            "company_user_create_failed_after_company",
            extra={
                "event": "company_user_create_failed_after_company",
                "user_id": current_user.id,
                "company_id": str(company.id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete signup",
        ) from exc

    LOGGER.info(
        "Company self-serve signup",
        extra={
            "user_id": current_user.id,
            "company_id": str(company.id),
            "company_user_id": str(company_user.id),
        },
    )

    return create_api_response(
        data=_project_company_user_profile(company_user, company),
        message="Company registered",
        request=request,
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
