"""Application-layer enums for status/lookup fields stored as String in Postgres.

DB columns stay `String` (Alembic-friendly, no `ALTER TYPE` migrations to add values).
These StrEnum classes give Python type safety + Pydantic validation at API layer.
Comments on model columns must stay in sync with values here.
"""

from enum import StrEnum

# ---------------------------------------------------------------------------
# Company / Recruiter onboarding (from screenshots)
# ---------------------------------------------------------------------------


class CompanyStage(StrEnum):
    """Funding stage of a Contrario client company."""

    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C = "series_c"
    GROWTH = "growth"


class RecruitedFundingStage(StrEnum):
    """Funding stage that a recruiter recruits for most (recruiter onboarding wizard)."""

    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C = "series_c"
    SERIES_D_PLUS = "series_d_plus"
    PUBLIC = "public"


class WorkspaceType(StrEnum):
    """Recruiter's working context (recruiter onboarding wizard)."""

    AGENCY = "agency"
    STARTUP = "startup"
    FREELANCE = "freelance"
    CORPORATE = "corporate"
    EXEC_SEARCH = "exec_search"


class CompanySizeRange(StrEnum):
    """Headcount band for client company (UI dropdown)."""

    XS = "1-10"
    SM = "11-50"
    MD = "51-200"
    LG = "201-1000"
    XL = "1001+"


# ---------------------------------------------------------------------------
# Job classification
# ---------------------------------------------------------------------------


class RoleCategory(StrEnum):
    ENGINEERING = "engineering"
    GTM = "gtm"
    DESIGN = "design"
    OPS = "ops"
    DATA = "data"


class Seniority(StrEnum):
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"


class RemoteOnsite(StrEnum):
    REMOTE = "remote"
    ONSITE = "onsite"
    HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------


class CompanyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CHURNED = "churned"


class RecruiterStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OperatorStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class CandidateStatus(StrEnum):
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    REVIEW_QUEUE = "review_queue"


class CandidateSource(StrEnum):
    SEED = "seed"
    RECRUITER_UPLOAD = "recruiter_upload"
    SOURCING_AGENT = "sourcing_agent"


class JobStatus(StrEnum):
    INTAKE = "intake"
    RECRUITER_ASSIGNMENT = "recruiter_assignment"
    SOURCING = "sourcing"
    SCORING = "scoring"
    REVIEW = "review"
    CLOSED = "closed"


class AssignmentStatus(StrEnum):
    RECOMMENDED = "recommended"
    OPERATOR_CONFIRMED = "operator_confirmed"
    REJECTED = "rejected"
    NOTIFIED = "notified"
    DECLINED_BY_RECRUITER = "declined_by_recruiter"


class SubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    SCORING = "scoring"
    SCORED = "scored"
    SHORTLISTED = "shortlisted"
    APPROVED = "approved"
    REJECTED = "rejected"


class CompanyUserRole(StrEnum):
    HIRING_MANAGER = "hiring_manager"
    ADMIN = "admin"


# ---------------------------------------------------------------------------
# HITL + workflow
# ---------------------------------------------------------------------------


class HitlSignalType(StrEnum):
    OPERATOR_APPROVAL = "operator_approval"
    COMPANY_REVIEW = "company_review"


class HitlActorType(StrEnum):
    OPERATOR = "operator"
    COMPANY_USER = "company_user"


class HitlAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REEVAL = "reeval"
    ADJUST = "adjust"


class WorkflowRunStatus(StrEnum):
    RUNNING = "running"
    PAUSED_HITL = "paused_hitl"
    COMPLETED = "completed"
    FAILED = "failed"


__all__ = [
    "CompanyStage",
    "RecruitedFundingStage",
    "WorkspaceType",
    "CompanySizeRange",
    "RoleCategory",
    "Seniority",
    "RemoteOnsite",
    "CompanyStatus",
    "RecruiterStatus",
    "OperatorStatus",
    "CandidateStatus",
    "CandidateSource",
    "JobStatus",
    "AssignmentStatus",
    "SubmissionStatus",
    "CompanyUserRole",
    "HitlSignalType",
    "HitlActorType",
    "HitlAction",
    "WorkflowRunStatus",
]
