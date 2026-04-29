"""add company_stage to recruiter_placements

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29 00:00:00.000000

Adds `company_stage` column to `recruiter_placements`. Values are constrained
at the application layer by the `CompanyStage` enum (`apps/backend/app/schemas/enums.py`):
seed | series_a | series_b | series_c | growth.

Captured via the recruiter onboarding wizard's "Add Placement" modal dropdown.
Feeds the recruiter indexing workflow's `PLACED_AT -> CompanyStage` Neo4j edges
and `placements_by_stage` derived metric.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add nullable company_stage column."""
    op.add_column(
        "recruiter_placements",
        sa.Column("company_stage", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema — drop company_stage column."""
    op.drop_column("recruiter_placements", "company_stage")
