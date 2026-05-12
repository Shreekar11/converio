"""Add operator_proposals and notifications tables for recruiter assignment HITL

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-06 00:00:00.000000

Wave 1 (Recruiter Assignment Agent) requires two new tables:

* `operator_proposals` — persisted recruiter-assignment proposals awaiting
  HITL #1 operator review. One row per proposal attempt; older rows are
  marked superseded when the agent re-proposes.
* `notifications` — outbound notification stub for the PoW. The
  `notify_assigned_recruiters` activity inserts one row per recruiter
  rather than dispatching to Slack/SMTP (post-PoW work).

Both tables use ON DELETE CASCADE on their FKs to jobs/recruiters since
they are tightly coupled to those parents.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — create operator_proposals and notifications tables."""
    op.create_table(
        "operator_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "quality_flag",
            sa.String(length=10),
            server_default="high",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Recruiter assignment proposals awaiting Converio operator HITL review",
    )
    op.create_index(
        "ix_operator_proposals_job_id_created_at",
        "operator_proposals",
        ["job_id", "created_at"],
        postgresql_ops={"created_at": "DESC"},
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recruiter_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column(
            "channel",
            sa.String(length=20),
            server_default="stub",
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="sent",
            nullable=False,
        ),
        sa.Column(
            "dispatched_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["recruiter_id"], ["recruiters.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Outbound notifications stub — recruiter assignment alerts (PoW)",
    )
    op.create_index(
        "ix_notifications_recruiter_dispatched",
        "notifications",
        ["recruiter_id", "dispatched_at"],
    )


def downgrade() -> None:
    """Downgrade schema — drop indexes and tables in reverse order."""
    op.execute("DROP INDEX IF EXISTS ix_notifications_recruiter_dispatched")
    op.execute("DROP TABLE IF EXISTS notifications CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_operator_proposals_job_id_created_at")
    op.execute("DROP TABLE IF EXISTS operator_proposals CASCADE")
