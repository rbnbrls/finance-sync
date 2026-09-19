"""Add tenant-scoped spending mappings, overrides and destination refs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def _common() -> list[sa.Column[object]]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("report", postgresql.JSONB(), nullable=True))
    op.create_table(
        "merchant_mappings", *_common(),
        sa.Column("merchant_key", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("category", sa.String(256), nullable=True),
        sa.Column("taxonomy", sa.String(128), nullable=True),
        sa.Column("normalization_version", sa.String(32), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "merchant_key", name="uq_merchant_mapping_key"),
    )
    op.create_table(
        "category_mappings", *_common(),
        sa.Column("taxonomy", sa.String(128), nullable=False),
        sa.Column("source_category", sa.String(256), nullable=False),
        sa.Column("destination_type", sa.String(64), nullable=False),
        sa.Column("destination_category", sa.String(256), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_table(
        "transaction_overrides", *_common(),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("provenance", sa.String(64), nullable=False, server_default="user"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_transaction_overrides_transaction_id", "transaction_overrides", ["transaction_id"])
    op.create_table(
        "destination_object_references", *_common(),
        sa.Column("destination_type", sa.String(64), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=False),
        sa.Column("destination_object_id", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False, server_default="write"),
        sa.Column("source_revision", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "destination_type", "idempotency_key", name="uq_destination_idempotency"),
    )
    op.create_index("ix_destination_object_references_transaction_id", "destination_object_references", ["transaction_id"])


def downgrade() -> None:
    op.drop_column("sync_runs", "report")
    op.drop_index("ix_destination_object_references_transaction_id", table_name="destination_object_references")
    op.drop_table("destination_object_references")
    op.drop_index("ix_transaction_overrides_transaction_id", table_name="transaction_overrides")
    op.drop_table("transaction_overrides")
    op.drop_table("category_mappings")
    op.drop_table("merchant_mappings")
