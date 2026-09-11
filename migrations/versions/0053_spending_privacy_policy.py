"""Add tenant-scoped spending privacy policy.

Revision ID: 0053
Revises: 0052
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spending_privacy_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled_fields", postgresql.JSONB(), nullable=False),
        sa.Column("retention_days", sa.Integer(), server_default="365", nullable=False),
        sa.Column("allow_attachments", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("allow_raw_payload", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("provenance", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_spending_privacy_policy_tenant"),
    )
    op.create_index(
        "ix_spending_privacy_policies_tenant_id",
        "spending_privacy_policies",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spending_privacy_policies_tenant_id",
        table_name="spending_privacy_policies",
    )
    op.drop_table("spending_privacy_policies")
