"""Persist sanitized aggregate destination parity evidence.

Revision ID: 0064
Revises: 0063
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "export_targets",
        sa.Column("last_parity_summary", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("export_targets", "last_parity_summary")
