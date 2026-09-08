"""Add tenant-scoped market-data exception decisions.

Revision ID: 0056
Revises: 0055
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_data_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "security_id", name="uq_market_data_exception"),
    )
    op.create_index("ix_market_data_exceptions_tenant_id", "market_data_exceptions", ["tenant_id"])
    op.create_index("ix_market_data_exceptions_security_id", "market_data_exceptions", ["security_id"])


def downgrade() -> None:
    op.drop_index("ix_market_data_exceptions_security_id", table_name="market_data_exceptions")
    op.drop_index("ix_market_data_exceptions_tenant_id", table_name="market_data_exceptions")
    op.drop_table("market_data_exceptions")
