"""Add scorecard audit columns (tool_call_count, total_cost_usd, updated_at)

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-21 00:00:00.000000

Wave 3 (Scorecard Generator Agent, Agent 4) requires three additional
columns on the existing `scorecards` table:

* `tool_call_count` — non-negative integer, count of evidence-fetch tool
  invocations the agent made while producing this scorecard. Drives cost
  attribution and the operator UI's "agent effort" indicator.
* `total_cost_usd` — Numeric(10, 4) for LLM + tool spend on this
  scorecard. Bounded precision chosen to avoid the implicit-cast pain of
  unscaled Numeric while leaving headroom for sub-cent rollups.
* `updated_at` — TIMESTAMP(WITH TIME ZONE), required by the upsert
  semantics in `ScorecardRepository.upsert_scorecard`, which uses
  `(job_id, candidate_id, rubric_id)` ON CONFLICT DO UPDATE and needs an
  explicit mtime for ordering "latest scorecard" reads.

Defaults are filled for existing rows (zero usage, NOW() for updated_at)
so the migration is safe to apply against environments that already have
scorecards persisted.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add audit columns to scorecards."""
    op.add_column(
        "scorecards",
        sa.Column(
            "tool_call_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "scorecards",
        sa.Column(
            "total_cost_usd",
            sa.Numeric(10, 4),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "scorecards",
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema — drop audit columns in reverse order."""
    op.drop_column("scorecards", "updated_at")
    op.drop_column("scorecards", "total_cost_usd")
    op.drop_column("scorecards", "tool_call_count")
