"""company status default to pending_review

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-03 00:00:00.000000

Self-serve company signups land in `pending_review` until an operator approves
them, so the `companies.status` column default flips from `active` to
`pending_review`. Allowed values are constrained at the application layer by
the `CompanyStatus` enum (`apps/backend/app/schemas/enums.py`):
pending_review | active | paused | churned.

Column type and nullability are unchanged — only the server-side default moves.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — set companies.status server_default to 'pending_review'."""
    op.alter_column(
        "companies",
        "status",
        existing_type=sa.String(length=20),
        server_default="pending_review",
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema — revert companies.status server_default to 'active'."""
    op.alter_column(
        "companies",
        "status",
        existing_type=sa.String(length=20),
        server_default="active",
        existing_nullable=False,
    )
