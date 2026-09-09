"""Add live progress and cancellation metadata to sync runs.

Revision ID: 0066
Revises: 0065
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("current_stage", sa.String(64), nullable=True))
    op.add_column("sync_runs", sa.Column("current_account_id", sa.String(128), nullable=True))
    op.add_column("sync_runs", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_runs", "last_activity_at")
    op.drop_column("sync_runs", "current_account_id")
    op.drop_column("sync_runs", "current_stage")
