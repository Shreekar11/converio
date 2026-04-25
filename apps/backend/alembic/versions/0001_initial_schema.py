"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-04-24 12:41:04.938623

"""
from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # pgvector extension — idempotent, also in infra/postgres/init-db.sql
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=True),
        sa.Column("industry", sa.String(), nullable=True),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Client orgs Contrario serves as managed recruiting service",
    )
    op.create_table(
        "operators",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("supabase_user_id", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("supabase_user_id"),
        comment="Contrario internal talent-ops — not tied to any company",
    )
    op.create_table(
        "recruiters",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("supabase_user_id", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column(
            "domain_expertise",
            sa.ARRAY(sa.String()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("acceptance_rate", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("avg_days_to_close", sa.Integer(), nullable=True),
        sa.Column("fill_rate_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("total_placements", sa.Integer(), nullable=False),
        sa.Column("at_capacity", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(384),
            nullable=True,
        ),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("supabase_user_id"),
        comment="Independent contractors vetted by Contrario",
    )
    op.create_table(
        "candidates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("github_username", sa.String(), nullable=True),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column("seniority", sa.String(length=20), nullable=True),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("stage_fit", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("work_history", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("education", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("github_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resume_text", sa.Text(), nullable=True),
        sa.Column("completeness_score", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(384),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=30), nullable=True),
        sa.Column("source_recruiter_id", sa.UUID(), nullable=True),
        sa.Column("dedup_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_recruiter_id"], ["recruiters.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_hash"),
        comment="Parsed resume + enrichment. One row per unique candidate across all roles",
    )
    op.create_table(
        "company_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("supabase_user_id", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supabase_user_id"),
        comment="Hiring-manager seats per company linked to Supabase auth",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("intake_notes", sa.Text(), nullable=True),
        sa.Column("role_category", sa.String(length=20), nullable=True),
        sa.Column("seniority_level", sa.String(length=20), nullable=True),
        sa.Column("stage_fit", sa.String(length=20), nullable=True),
        sa.Column("remote_onsite", sa.String(length=10), nullable=True),
        sa.Column("must_have_skills", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("nice_to_have_skills", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("compensation_min", sa.Integer(), nullable=True),
        sa.Column("compensation_max", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["company_users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Managed role intake — root Temporal JobIntakeWorkflow entity",
    )
    op.create_index("ix_jobs_workflow_id", "jobs", ["workflow_id"], unique=False)
    op.create_table(
        "assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("recruiter_id", sa.UUID(), nullable=False),
        sa.Column("ai_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("ai_rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("operator_override", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("confirmed_by_operator_id", sa.UUID(), nullable=True),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_operator_id"], ["operators.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recruiter_id"], ["recruiters.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "recruiter_id", name="uq_assignments_job_recruiter"),
        comment="Recruiter-to-job assignment from Agent 0 recruiter matching + operator HITL",
    )
    op.create_table(
        "candidate_submissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("recruiter_id", sa.UUID(), nullable=False),
        sa.Column("resume_storage_url", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recruiter_id"], ["recruiters.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "candidate_id", name="uq_candidate_submissions_job_candidate"
        ),
        comment="Recruiter resume submission per (job, candidate) pair",
    )
    op.create_table(
        "hitl_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("signal_type", sa.String(length=30), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Audit log for both HITL signal points: operator_approval and company_review",
    )
    op.create_table(
        "rubrics",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "version", name="uq_rubrics_job_version"),
        comment="Weighted scoring rubric per job, versioned for reeval support",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_type", sa.String(length=60), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("candidate_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        comment="Temporal workflow execution rows — SSE status + FE polling fallback",
    )
    op.create_index(
        "ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"], unique=True
    )
    op.create_table(
        "scorecards",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=True),
        sa.Column("rubric_id", sa.UUID(), nullable=False),
        sa.Column("overall_match_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("strengths", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("red_flags", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("self_correction_triggered", sa.Boolean(), nullable=False),
        sa.Column("dimensions_rescored", sa.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rubric_id"], ["rubrics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["candidate_submissions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "candidate_id", "rubric_id", name="uq_scorecards_job_candidate_rubric"
        ),
        comment="Agent 4 scorecard output, pinned to rubric version for reeval history",
    )

    # ------------------------------------------------------------------ #
    # Semantic search indexes (not auto-generated by Alembic)             #
    # ------------------------------------------------------------------ #

    # Candidate embedding — ivfflat cosine similarity (sentence-transformers 384-dim)
    op.create_index(
        "ix_candidates_embedding",
        "candidates",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_with={"lists": 100},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # Recruiter embedding — smaller lists (recruiter pool ~25–250)
    op.create_index(
        "ix_recruiters_embedding",
        "recruiters",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_with={"lists": 50},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # GIN index on candidates.skills JSONB for skill-level filtering
    op.execute(
        "CREATE INDEX ix_candidates_skills_gin ON candidates USING gin (skills jsonb_path_ops)"
    )

    # GIN index on recruiters.domain_expertise text array
    op.execute(
        "CREATE INDEX ix_recruiters_domain_gin ON recruiters USING gin (domain_expertise)"
    )

    # Partial unique index on candidates.github_username — only when not null
    op.execute(
        "CREATE UNIQUE INDEX ix_candidates_github_username "
        "ON candidates (github_username) WHERE github_username IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema — drop in reverse dependency order."""
    # Custom indexes — IF EXISTS guards against partial state (e.g. test teardown dropped tables)
    op.execute("DROP INDEX IF EXISTS ix_candidates_github_username")
    op.execute("DROP INDEX IF EXISTS ix_recruiters_domain_gin")
    op.execute("DROP INDEX IF EXISTS ix_candidates_skills_gin")
    op.execute("DROP INDEX IF EXISTS ix_recruiters_embedding")
    op.execute("DROP INDEX IF EXISTS ix_candidates_embedding")

    # Tables (reverse FK order) — IF EXISTS guards against partial state
    op.execute("DROP TABLE IF EXISTS scorecards CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_workflow_runs_workflow_id")
    op.execute("DROP TABLE IF EXISTS workflow_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS rubrics CASCADE")
    op.execute("DROP TABLE IF EXISTS hitl_events CASCADE")
    op.execute("DROP TABLE IF EXISTS candidate_submissions CASCADE")
    op.execute("DROP TABLE IF EXISTS assignments CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_jobs_workflow_id")
    op.execute("DROP TABLE IF EXISTS jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS company_users CASCADE")
    op.execute("DROP TABLE IF EXISTS candidates CASCADE")
    op.execute("DROP TABLE IF EXISTS recruiters CASCADE")
    op.execute("DROP TABLE IF EXISTS operators CASCADE")
    op.execute("DROP TABLE IF EXISTS companies CASCADE")
    # Leave vector extension — shared with future migrations
