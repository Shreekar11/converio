"""screenshot_fields_and_recruiter_credibility

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-25 10:05:17.297795

Data model changes:

- companies: logo_url, company_size_range, founding_year, hq_location, description
- recruiters: linkedin_url, bio, recruited_funding_stage, workspace_type
- candidates: phone
- jobs: location_text

Plus two recruiter-credibility tables (Agent 0 fit-scoring inputs):
- recruiter_clients (past external clients)
- recruiter_placements (past placements, historical claims)
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — purely additive on top of 0001."""
    # ---------------------------------------------------------- new tables
    op.create_table(
        "recruiter_clients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recruiter_id", sa.UUID(), nullable=False),
        sa.Column("client_company_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role_focus", sa.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recruiter_id"], ["recruiters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Recruiter onboarding credibility — past external clients",
    )
    op.create_table(
        "recruiter_placements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recruiter_id", sa.UUID(), nullable=False),
        sa.Column("candidate_name", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("role_title", sa.String(), nullable=False),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default="NOW()",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recruiter_id"], ["recruiters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="Recruiter onboarding credibility — past placements (historical claims)",
    )

    # ---------------------------------------------------------- companies
    op.add_column("companies", sa.Column("logo_url", sa.String(), nullable=True))
    op.add_column(
        "companies", sa.Column("company_size_range", sa.String(length=20), nullable=True)
    )
    op.add_column("companies", sa.Column("founding_year", sa.Integer(), nullable=True))
    op.add_column("companies", sa.Column("hq_location", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("description", sa.Text(), nullable=True))

    # ---------------------------------------------------------- recruiters
    op.add_column("recruiters", sa.Column("linkedin_url", sa.String(), nullable=True))
    op.add_column("recruiters", sa.Column("bio", sa.Text(), nullable=True))
    op.add_column(
        "recruiters",
        sa.Column("recruited_funding_stage", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "recruiters", sa.Column("workspace_type", sa.String(length=30), nullable=True)
    )

    # ---------------------------------------------------------- candidates
    op.add_column("candidates", sa.Column("phone", sa.String(length=40), nullable=True))

    # ---------------------------------------------------------- jobs
    op.add_column("jobs", sa.Column("location_text", sa.String(), nullable=True))

    # NOTE: Autogenerate flagged 5 custom indexes (ix_candidates_embedding,
    # ix_candidates_github_username, ix_candidates_skills_gin, ix_recruiters_embedding,
    # ix_recruiters_domain_gin) for drop. These are FALSE POSITIVES — they live in 0001
    # via op.execute()/op.create_index() and Alembic's reflection cannot see them.
    # Do NOT drop them here.


def downgrade() -> None:
    """Downgrade schema — reverse of upgrade."""
    op.drop_column("jobs", "location_text")
    op.drop_column("candidates", "phone")

    op.drop_column("recruiters", "workspace_type")
    op.drop_column("recruiters", "recruited_funding_stage")
    op.drop_column("recruiters", "bio")
    op.drop_column("recruiters", "linkedin_url")

    op.drop_column("companies", "description")
    op.drop_column("companies", "hq_location")
    op.drop_column("companies", "founding_year")
    op.drop_column("companies", "company_size_range")
    op.drop_column("companies", "logo_url")

    op.execute("DROP TABLE IF EXISTS recruiter_placements CASCADE")
    op.execute("DROP TABLE IF EXISTS recruiter_clients CASCADE")
