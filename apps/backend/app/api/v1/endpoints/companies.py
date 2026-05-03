"""Operator-only company onboarding endpoints (Phase B of Job Intake).

POST   /api/v1/companies                       — create client company
GET    /api/v1/companies                       — list (paginated)
GET    /api/v1/companies/{company_id}          — detail w/ seated users
POST   /api/v1/companies/{company_id}/users    — provision hiring-manager seat
GET    /api/v1/companies/{company_id}/users    — list seated users

All five operations are gated by `get_current_operator` — non-operator
callers receive 403 well before any DB IO. Request / response shapes are
imported from `app.schemas.generated.companies` (codegen output of
`app/api/v1/specs/companies.json`); the spec is the source of truth.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_operator
from app.core.database import get_async_session
from app.database.models import Company, CompanyUser, Operator
from app.repositories.companies import CompanyRepository
from app.repositories.company_users import CompanyUserRepository
from app.schemas.enums import CompanyStatus
from app.schemas.generated.companies import (
    CompaniesListResponse,
    CompanyCreate,
    CompanyDetailResponse,
    CompanyResponse,
    CompanyStatusUpdate,
    CompanyUserCreate,
    CompanyUserResponse,
    CompanyUsersListResponse,
)
from app.utils.logging import get_logger
from app.utils.responses import ApiResponse, create_api_response

router = APIRouter()
LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# Status transition policy
# ---------------------------------------------------------------------------
#
# Module-level constant so the policy is auditable in one place and unit
# tests can import it without spinning up the FastAPI app. Every legal
# operator-driven status mutation is encoded here; anything not listed is
# rejected with HTTP 422 by `update_company_status` below.
#
#   pending_review -> active   (operator approves a freshly self-served signup)
#   pending_review -> churned  (operator rejects a freshly self-served signup)
#   active         -> paused   (temporarily disable an onboarded company)
#   active         -> churned  (permanent off-boarding from active state)
#   paused         -> active   (re-enable a previously paused company)
#   paused         -> churned  (permanent off-boarding from paused state)
#
# `churned` is a terminal state by design — re-onboarding requires creating
# a new company record so audit trails remain intact.

VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"active", "churned"},
    "active": {"paused", "churned"},
    "paused": {"active", "churned"},
}


# ---------------------------------------------------------------------------
# Projection helpers (ORM row -> generated Pydantic response model)
# ---------------------------------------------------------------------------
#
# Centralised so the five endpoints below stay free of ad-hoc field plucking.
# `model_dump(mode="json")` on the result coerces UUID + datetime values to
# the JSON-friendly form the API envelope expects.


def _project_company(company: Company) -> CompanyResponse:
    """Map a `Company` ORM row onto the generated `CompanyResponse`."""
    return CompanyResponse.model_validate(
        {
            "id": company.id,
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
            "created_at": company.created_at,
            "updated_at": company.updated_at,
        }
    )


def _project_company_user(user: CompanyUser) -> CompanyUserResponse:
    """Map a `CompanyUser` ORM row onto the generated `CompanyUserResponse`."""
    return CompanyUserResponse.model_validate(
        {
            "id": user.id,
            "company_id": user.company_id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        }
    )


def _project_company_detail(
    company: Company, users: list[CompanyUser]
) -> CompanyDetailResponse:
    """Map a `Company` + its seated users onto `CompanyDetailResponse`."""
    return CompanyDetailResponse.model_validate(
        {
            "id": company.id,
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
            "created_at": company.created_at,
            "updated_at": company.updated_at,
            "users": [_project_company_user(u) for u in users],
        }
    )


# ---------------------------------------------------------------------------
# B1 — POST /companies
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a client company",
    operation_id="create_company",
)
async def create_company(
    request: Request,
    payload: CompanyCreate,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Create a new client company. Operator-only.

    Duplicate name (case-insensitive) returns 409 — the OpenAPI spec marks
    `name` as case-insensitively unique across Converio. We never echo the
    submitted name back in the error detail to avoid reflecting raw
    operator input.
    """
    repo = CompanyRepository(session)

    existing = await repo.get_by_name_ci(payload.name)
    if existing is not None:
        LOGGER.warning(
            "Duplicate company name on create",
            extra={
                "operator_id": str(operator.id),
                "existing_company_id": str(existing.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company with this name already exists",
        )

    try:
        # `website` / `logo_url` come back from the generated model as
        # `AnyUrl`; coerce to plain strings for the String columns.
        company = await repo.create(
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
            status=CompanyStatus.ACTIVE.value,
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to create company",
            extra={"operator_id": str(operator.id), "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create company",
        ) from exc

    LOGGER.info(
        "Company created",
        extra={
            "operator_id": str(operator.id),
            "company_id": str(company.id),
        },
    )

    return create_api_response(
        data=_project_company(company).model_dump(mode="json"),
        message="Company created",
        request=request,
    )


# ---------------------------------------------------------------------------
# B3 — GET /companies (paginated)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="List companies (paginated)",
    operation_id="list_companies",
)
async def list_companies(
    request: Request,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse:
    """Return a paginated list of companies. Operator-only."""
    repo = CompanyRepository(session)

    try:
        rows, total = await repo.list_paginated(limit=limit, offset=offset)
    except Exception as exc:
        LOGGER.exception(
            "Failed to list companies",
            extra={"operator_id": str(operator.id), "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list companies",
        ) from exc

    payload = CompaniesListResponse(
        data=[_project_company(c) for c in rows],
        limit=limit,
        offset=offset,
        total=total,
    )

    LOGGER.info(
        "Listed companies",
        extra={
            "operator_id": str(operator.id),
            "limit": limit,
            "offset": offset,
            "total": total,
        },
    )

    return create_api_response(
        data=payload.model_dump(mode="json"),
        message="Companies fetched",
        request=request,
    )


# ---------------------------------------------------------------------------
# B3 — GET /companies/{company_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{company_id}",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a single company by id with seated users",
    operation_id="get_company",
)
async def get_company(
    request: Request,
    company_id: UUID,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Return company detail with eagerly-loaded seated users. Operator-only."""
    repo = CompanyRepository(session)

    try:
        company = await repo.get_with_users(company_id)
    except Exception as exc:
        LOGGER.exception(
            "Failed to fetch company",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch company",
        ) from exc

    if company is None:
        LOGGER.info(
            "Company not found",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    LOGGER.info(
        "Fetched company",
        extra={
            "operator_id": str(operator.id),
            "company_id": str(company.id),
            "user_count": len(company.users),
        },
    )

    return create_api_response(
        data=_project_company_detail(company, list(company.users)).model_dump(
            mode="json"
        ),
        message="Company fetched",
        request=request,
    )


# ---------------------------------------------------------------------------
# B2 — POST /companies/{company_id}/users
# ---------------------------------------------------------------------------


@router.post(
    "/{company_id}/users",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a hiring-manager or admin seat for a company",
    operation_id="provision_company_user",
)
async def provision_company_user(
    request: Request,
    company_id: UUID,
    payload: CompanyUserCreate,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Create a `company_users` row with `supabase_user_id=null`.

    Supabase fills the user id on first login. Operator-only. Duplicate email
    (regardless of company) returns 409 — `email` is treated as a global
    handle in the seat-provisioning UX so a single human cannot be seated
    twice across the platform.
    """
    company_repo = CompanyRepository(session)
    user_repo = CompanyUserRepository(session)

    company = await company_repo.get_by_id(company_id)
    if company is None:
        LOGGER.info(
            "Provision seat: company not found",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    existing_user = await user_repo.get_by_email(payload.email)
    if existing_user is not None:
        LOGGER.warning(
            "Provision seat: duplicate email",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "existing_user_id": str(existing_user.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        )

    try:
        user = await user_repo.create(
            company_id=company_id,
            email=payload.email,
            full_name=payload.full_name,
            role=payload.role.value,
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to provision company user",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to provision company user",
        ) from exc

    LOGGER.info(
        "Company user provisioned",
        extra={
            "operator_id": str(operator.id),
            "company_id": str(company_id),
            "company_user_id": str(user.id),
            "role": user.role,
        },
    )

    return create_api_response(
        data=_project_company_user(user).model_dump(mode="json"),
        message="Company user provisioned",
        request=request,
    )


# ---------------------------------------------------------------------------
# B3 — GET /companies/{company_id}/users
# ---------------------------------------------------------------------------


@router.get(
    "/{company_id}/users",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="List seated users for a company",
    operation_id="list_company_users",
)
async def list_company_users(
    request: Request,
    company_id: UUID,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Return the seated hiring-manager / admin users for a company.

    404 if the company does not exist (lets the operator console distinguish
    "company gone" from "no seats yet"). Operator-only.
    """
    company_repo = CompanyRepository(session)
    user_repo = CompanyUserRepository(session)

    company = await company_repo.get_by_id(company_id)
    if company is None:
        LOGGER.info(
            "List company users: company not found",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    try:
        users = await user_repo.list_for_company(company_id)
    except Exception as exc:
        LOGGER.exception(
            "Failed to list company users",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list company users",
        ) from exc

    payload = CompanyUsersListResponse(
        data=[_project_company_user(u) for u in users]
    )

    LOGGER.info(
        "Listed company users",
        extra={
            "operator_id": str(operator.id),
            "company_id": str(company_id),
            "user_count": len(users),
        },
    )

    return create_api_response(
        data=payload.model_dump(mode="json"),
        message="Company users fetched",
        request=request,
    )


# ---------------------------------------------------------------------------
# T4.1 — PATCH /companies/{company_id}/status (operator approval flow)
# ---------------------------------------------------------------------------


@router.patch(
    "/{company_id}/status",
    response_model=ApiResponse,
    status_code=status.HTTP_200_OK,
    summary="Update company status (operator approval flow)",
    operation_id="update_company_status",
)
async def update_company_status(
    request: Request,
    company_id: UUID,
    payload: CompanyStatusUpdate,
    operator: Annotated[Operator, Depends(get_current_operator)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ApiResponse:
    """Mutate a company's lifecycle `status` along an allowed transition.

    Drives the self-serve auth approval flow: a company freshly created in
    `pending_review` is promoted to `active` (or rejected to `churned`) by
    an operator; an already-active company can be `paused`/`churned`, and
    a paused company can be re-activated or churned. Any other transition
    is rejected with HTTP 422 — the contract is centralised in
    `VALID_STATUS_TRANSITIONS` at the top of this module.

    Operator-only. Audit log emits `from_status` + `to_status` (no PII)
    keyed by operator id and company id.
    """
    repo = CompanyRepository(session)

    company = await repo.get_by_id(company_id)
    if company is None:
        LOGGER.info(
            "Update company status: company not found",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    current_status = company.status
    target_status = payload.status.value

    if (
        current_status not in VALID_STATUS_TRANSITIONS
        or target_status not in VALID_STATUS_TRANSITIONS.get(current_status, set())
    ):
        LOGGER.warning(
            "Rejected invalid status transition",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "from_status": current_status,
                "to_status": target_status,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid status transition",
        )

    try:
        updated_company = await repo.update_status(company_id, target_status)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception(
            "Failed to update company status",
            extra={
                "operator_id": str(operator.id),
                "company_id": str(company_id),
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update company status",
        ) from exc

    LOGGER.info(
        "Company status updated",
        extra={
            "operator_id": str(operator.id),
            "company_id": str(company_id),
            "from_status": current_status,
            "to_status": target_status,
        },
    )

    return create_api_response(
        data=_project_company(updated_company).model_dump(mode="json"),
        message="Company status updated",
        request=request,
    )
