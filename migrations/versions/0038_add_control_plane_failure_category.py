"""Persist the stable category of a failed sync run."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("error_category", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sync_runs", "error_category")
