"""Store the Wealthfolio preflight contract on export runs.

Revision ID: 0062
Revises: 0061
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "export_runs",
        sa.Column("preflight_manifest", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("export_runs", "preflight_manifest")
