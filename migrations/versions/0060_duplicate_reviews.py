"""Store explicit user decisions for duplicate transaction findings.

Revision ID: 0060
Revises: 0059
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "duplicate_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id_a", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id_b", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pair_key", sa.String(length=80), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("kept_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["transaction_id_a"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id_b"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kept_transaction_id"], ["transactions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "pair_key", name="uq_duplicate_review_pair"),
    )
    op.create_index("ix_duplicate_reviews_tenant_id", "duplicate_reviews", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_duplicate_reviews_tenant_id", table_name="duplicate_reviews")
    op.drop_table("duplicate_reviews")
