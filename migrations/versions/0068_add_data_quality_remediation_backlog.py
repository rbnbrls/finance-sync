"""Add durable data-quality remediation backlog."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_quality_remediation_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("issue_type", sa.String(64), nullable=False),
        sa.Column("affected_entity_type", sa.String(64), nullable=False),
        sa.Column("affected_entity_id", sa.String(256), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column(
            "first_detected_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "rate_limit_deferral_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("remediation_strategy", sa.String(96), nullable=False),
        sa.Column("deduplication_key", sa.String(128), nullable=False),
        sa.Column("batch_key", sa.String(256), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_category", sa.String(32), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verification_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["credentials.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "deduplication_key",
            name="uq_dq_remediation_tenant_dedup",
        ),
    )
    op.create_index(
        "ix_dq_remediation_due",
        "data_quality_remediation_items",
        ["status", "next_attempt_at", "priority", "first_detected_at"],
    )
    op.create_index(
        "ix_dq_remediation_tenant_status",
        "data_quality_remediation_items",
        ["tenant_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "ix_dq_remediation_provider_status",
        "data_quality_remediation_items",
        ["provider_key", "status", "next_attempt_at"],
    )
    op.create_index(
        "ix_dq_remediation_lease",
        "data_quality_remediation_items",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_dq_remediation_batch",
        "data_quality_remediation_items",
        ["tenant_id", "batch_key", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("data_quality_remediation_items")
