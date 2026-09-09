"""Add append-only canonical transaction lifecycle events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_lifecycle_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("provenance", sa.String(64), nullable=False, server_default="provider_sync"),
        sa.Column("source_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "transaction_id", "idempotency_key", name="uq_transaction_event_idempotency"),
    )
    op.create_index("ix_transaction_lifecycle_events_transaction_id", "transaction_lifecycle_events", ["transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_transaction_lifecycle_events_transaction_id", table_name="transaction_lifecycle_events")
    op.drop_table("transaction_lifecycle_events")
