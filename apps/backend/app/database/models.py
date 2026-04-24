"""SQLAlchemy models for all Converio Match database tables."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    TIMESTAMP,
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    pass


class Company(Base):
    """Client org that engages Contrario as managed recruiting service."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # seed | series_a | series_b | growth
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active | paused | churned
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    users: Mapped[list["CompanyUser"]] = relationship(
        "CompanyUser", back_populates="company", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="company", cascade="all, delete-orphan"
    )

    __table_args__ = ({"comment": "Client orgs Contrario serves as managed recruiting service"},)


class CompanyUser(Base):
    """Hiring-manager seats per company. supabase_user_id is soft FK to Supabase auth.users."""

    __tablename__ = "company_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # Soft reference to Supabase auth.users — no hard FK across schema boundaries
    supabase_user_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="hiring_manager"
    )  # hiring_manager | admin
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="users")
    jobs_created: Mapped[list["Job"]] = relationship("Job", back_populates="created_by_user")
    hitl_events: Mapped[list["HitlEvent"]] = relationship(
        "HitlEvent",
        primaryjoin="and_(HitlEvent.actor_id == foreign(CompanyUser.id), HitlEvent.actor_type == 'company_user')",
        viewonly=True,
    )

    __table_args__ = (
        {"comment": "Hiring-manager seats per company linked to Supabase auth"},
    )


class Operator(Base):
    """Contrario internal talent-ops team member. Not tied to any company."""

    __tablename__ = "operators"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Soft reference to Supabase auth.users — no hard FK across schema boundaries
    supabase_user_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active | inactive
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    confirmed_assignments: Mapped[list["Assignment"]] = relationship(
        "Assignment", back_populates="confirmed_by_operator"
    )

    __table_args__ = ({"comment": "Contrario internal talent-ops — not tied to any company"},)


class Recruiter(Base):
    """Independent contractor vetted by Contrario. Not tied to any company — brokered."""

    __tablename__ = "recruiters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Soft reference to Supabase auth.users — null until recruiter first logs in
    supabase_user_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    domain_expertise: Mapped[list | None] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )  # ['engineering','fintech','gtm',...]
    acceptance_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True
    )  # 0.000–1.000
    avg_days_to_close: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    total_placements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    at_capacity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending | active | suspended
    embedding: Mapped[Vector | None] = mapped_column(
        Vector(384), nullable=True
    )  # sentence-transformers all-MiniLM-L6-v2
    extra: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )  # raw placement history, past roles
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    sourced_candidates: Mapped[list["Candidate"]] = relationship(
        "Candidate", back_populates="source_recruiter"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        "Assignment", back_populates="recruiter"
    )
    submissions: Mapped[list["CandidateSubmission"]] = relationship(
        "CandidateSubmission", back_populates="recruiter"
    )

    __table_args__ = ({"comment": "Independent contractors vetted by Contrario"},)


class Candidate(Base):
    """Parsed resume + enrichment. One row per unique candidate; multiple submissions reuse."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)  # recruiter upload may omit
    github_username: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # partial unique index: WHERE github_username IS NOT NULL
    linkedin_url: Mapped[str | None] = mapped_column(String, nullable=True)
    seniority: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # junior | mid | senior | staff | principal
    years_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    stage_fit: Mapped[list | None] = mapped_column(
        ARRAY(String), nullable=True
    )  # ['seed','series_a',...]
    skills: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # [{name, depth: claimed|evidenced_projects|evidenced_commits}]
    work_history: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # [{company, role, start, end}]
    education: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    github_signals: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )  # {repo_count, top_language, commits_12m, stars_total}
    resume_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # raw text for citation resolver
    completeness_score: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=0
    )  # 0–1; <0.5 triggers human review signal
    embedding: Mapped[Vector | None] = mapped_column(
        Vector(384), nullable=True
    )  # sentence-transformers all-MiniLM-L6-v2
    source: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )  # seed | recruiter_upload | sourcing_agent
    source_recruiter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recruiters.id", ondelete="SET NULL"),
        nullable=True,
    )
    dedup_hash: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )  # lower(name)+email normalized — entity-resolution key
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="indexing"
    )  # indexing | indexed | failed | review_queue
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    source_recruiter: Mapped["Recruiter | None"] = relationship(
        "Recruiter", back_populates="sourced_candidates"
    )
    submissions: Mapped[list["CandidateSubmission"]] = relationship(
        "CandidateSubmission", back_populates="candidate", cascade="all, delete-orphan"
    )
    scorecards: Mapped[list["Scorecard"]] = relationship(
        "Scorecard", back_populates="candidate", cascade="all, delete-orphan"
    )
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        "WorkflowRun", back_populates="candidate"
    )

    __table_args__ = (
        {
            "comment": "Parsed resume + enrichment. One row per unique candidate across all roles"
        },
    )


class Job(Base):
    """Managed intake per role. Root Temporal workflow entity."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    intake_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # operator's onboarding-call notes
    role_category: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # engineering | gtm | design | ops | data
    seniority_level: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # junior | mid | senior | staff | principal
    stage_fit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    remote_onsite: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )  # remote | onsite | hybrid
    must_have_skills: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True)
    nice_to_have_skills: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True)
    compensation_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compensation_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="intake"
    )  # intake | recruiter_assignment | sourcing | scoring | review | closed
    workflow_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )  # Temporal root workflow id
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="jobs")
    created_by_user: Mapped["CompanyUser | None"] = relationship(
        "CompanyUser", back_populates="jobs_created"
    )
    rubrics: Mapped[list["Rubric"]] = relationship(
        "Rubric", back_populates="job", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        "Assignment", back_populates="job", cascade="all, delete-orphan"
    )
    submissions: Mapped[list["CandidateSubmission"]] = relationship(
        "CandidateSubmission", back_populates="job", cascade="all, delete-orphan"
    )
    scorecards: Mapped[list["Scorecard"]] = relationship(
        "Scorecard", back_populates="job", cascade="all, delete-orphan"
    )
    hitl_events: Mapped[list["HitlEvent"]] = relationship(
        "HitlEvent", back_populates="job", cascade="all, delete-orphan"
    )
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        "WorkflowRun", back_populates="job"
    )

    __table_args__ = ({"comment": "Managed role intake — root Temporal JobIntakeWorkflow entity"},)


class Rubric(Base):
    """Evaluation rubric generated per job by Agent 1. Versioned for company reeval support."""

    __tablename__ = "rubrics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # bumped on company reeval (HITL #2)
    dimensions: Mapped[list] = mapped_column(
        JSONB, nullable=False
    )  # [{name, description, weight, evaluation_guidance}]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="rubrics")
    scorecards: Mapped[list["Scorecard"]] = relationship(
        "Scorecard", back_populates="rubric"
    )

    __table_args__ = (
        UniqueConstraint("job_id", "version", name="uq_rubrics_job_version"),
        {"comment": "Weighted scoring rubric per job, versioned for reeval support"},
    )


class Assignment(Base):
    """Recruiter ↔ job linkage from Agent 0 + HITL #1 operator approval."""

    __tablename__ = "assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    recruiter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recruiters.id", ondelete="RESTRICT"), nullable=False
    )
    ai_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )  # null when operator overrides outside AI top-N
    ai_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    operator_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # true when operator picks recruiter not in AI top-N
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="recommended"
    )  # recommended | operator_confirmed | rejected | notified | declined_by_recruiter
    confirmed_by_operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("operators.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="assignments")
    recruiter: Mapped["Recruiter"] = relationship("Recruiter", back_populates="assignments")
    confirmed_by_operator: Mapped["Operator | None"] = relationship(
        "Operator", back_populates="confirmed_assignments"
    )

    __table_args__ = (
        UniqueConstraint("job_id", "recruiter_id", name="uq_assignments_job_recruiter"),
        {"comment": "Recruiter-to-job assignment from Agent 0 recruiter matching + operator HITL"},
    )


class CandidateSubmission(Base):
    """Recruiter submits specific candidate to specific job. Sourcing-agent candidates have no row."""

    __tablename__ = "candidate_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    recruiter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recruiters.id", ondelete="RESTRICT"), nullable=False
    )
    resume_storage_url: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Supabase storage path for original upload
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="submitted"
    )  # submitted | scoring | scored | shortlisted | approved | rejected
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="submissions")
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="submissions")
    recruiter: Mapped["Recruiter"] = relationship("Recruiter", back_populates="submissions")
    scorecard: Mapped["Scorecard | None"] = relationship(
        "Scorecard", back_populates="submission", uselist=False
    )

    __table_args__ = (
        UniqueConstraint(
            "job_id", "candidate_id", name="uq_candidate_submissions_job_candidate"
        ),
        {"comment": "Recruiter resume submission per (job, candidate) pair"},
    )


class Scorecard(Base):
    """Agent 4 output. Pinned to rubric version — reeval bumps rubric, creates new row."""

    __tablename__ = "scorecards"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_submissions.id", ondelete="SET NULL"),
        nullable=True,
    )  # null for sourcing-agent candidates (no recruiter submission)
    rubric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rubrics.id", ondelete="RESTRICT"), nullable=False
    )
    overall_match_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    dimensions: Mapped[list] = mapped_column(
        JSONB, nullable=False
    )  # [{name, score, confidence, weight, rationale, citation}]
    strengths: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True)
    red_flags: Mapped[list | None] = mapped_column(ARRAY(String), nullable=True)
    self_correction_triggered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    dimensions_rescored: Mapped[list | None] = mapped_column(
        ARRAY(String), nullable=True
    )  # names of dimensions that went through self-correction
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="scorecards")
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="scorecards")
    submission: Mapped["CandidateSubmission | None"] = relationship(
        "CandidateSubmission", back_populates="scorecard"
    )
    rubric: Mapped["Rubric"] = relationship("Rubric", back_populates="scorecards")

    __table_args__ = (
        UniqueConstraint(
            "job_id", "candidate_id", "rubric_id", name="uq_scorecards_job_candidate_rubric"
        ),
        {"comment": "Agent 4 scorecard output, pinned to rubric version for reeval history"},
    )


class HitlEvent(Base):
    """Audit log for both HITL signal points: operator_approval and company_review."""

    __tablename__ = "hitl_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    signal_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # operator_approval | company_review
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # operator | company_user
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )  # polymorphic — no hard FK; operator.id or company_user.id
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # approve | reject | reeval | adjust
    payload: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )  # {confirmed_recruiter_ids[]} OR {candidate_id, reason} OR {updated_rubric_weights}
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)  # Temporal wf signaled
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="hitl_events")

    __table_args__ = (
        {"comment": "Audit log for both HITL signal points: operator_approval and company_review"},
    )


class WorkflowRun(Base):
    """Observability row per Temporal workflow execution. FE reads for status alongside SSE."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )  # Temporal workflow id
    workflow_type: Mapped[str] = mapped_column(
        String(60), nullable=False
    )  # JobIntakeWorkflow | CandidateIndexingWorkflow | ...
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )  # running | paused_hitl | completed | failed
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="NOW()", nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    job: Mapped["Job | None"] = relationship("Job", back_populates="workflow_runs")
    candidate: Mapped["Candidate | None"] = relationship(
        "Candidate", back_populates="workflow_runs"
    )

    __table_args__ = (
        {"comment": "Temporal workflow execution rows — SSE status + FE polling fallback"},
    )


__all__ = [
    "Base",
    "Company",
    "CompanyUser",
    "Operator",
    "Recruiter",
    "Candidate",
    "Job",
    "Rubric",
    "Assignment",
    "CandidateSubmission",
    "Scorecard",
    "HitlEvent",
    "WorkflowRun",
]
