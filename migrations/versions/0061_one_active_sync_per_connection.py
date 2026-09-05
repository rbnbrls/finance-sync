"""Allow at most one active sync run per connection.

Revision ID: 0061
Revises: 0060
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "uq_sync_runs_active_connection",
        "sync_runs",
        ["connection_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'running' AND connection_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_runs_active_connection", table_name="sync_runs")
