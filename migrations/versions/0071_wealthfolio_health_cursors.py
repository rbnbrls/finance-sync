"""Persist aggregate Wealthfolio health polling state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wealthfolio_health_cursors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_successful_poll", sa.DateTime(timezone=True)),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_category", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["export_targets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "target_id", name="uq_wealthfolio_health_cursor_tenant_target"
        ),
    )
    op.create_index(
        "ix_wealthfolio_health_cursor_tenant_success",
        "wealthfolio_health_cursors",
        ["tenant_id", "last_successful_poll"],
    )
    op.create_index(
        "ix_wealthfolio_health_cursor_target",
        "wealthfolio_health_cursors",
        ["target_id"],
    )


def downgrade() -> None:
    op.drop_table("wealthfolio_health_cursors")
