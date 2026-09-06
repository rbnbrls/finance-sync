"""Add durable outbox publisher claims.

Revision ID: 0063
Revises: 0062
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_messages",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 0009 already creates a pending-only index with this name.  Replace it
    # so stale processing claims can use the same queue index as pending rows.
    # A clean schema can arrive here without the legacy partial index (for
    # example after a branch-specific migration history).  The replacement is
    # still authoritative, so make the transition idempotent.
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_outbox_messages_status_created"
        )
    )
    op.create_index(
        "ix_outbox_messages_status_created",
        "outbox_messages",
        ["status", "created_at"],
        postgresql_where=sa.text(
            "status IN ('pending', 'processing')"
        ),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_outbox_messages_status_created"
        )
    )
    op.create_index(
        "ix_outbox_messages_status_created",
        "outbox_messages",
        ["status", "created_at"],
        if_not_exists=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("outbox_messages", "claimed_at")
