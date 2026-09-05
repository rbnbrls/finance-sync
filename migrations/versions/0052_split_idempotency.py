"""Add retry-safe identity to transaction splits.

Revision ID: 0052
Revises: 0051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transaction_splits",
        sa.Column("idempotency_key", sa.String(length=256), nullable=True),
    )
    op.create_unique_constraint(
        "uq_transaction_split_idempotency",
        "transaction_splits",
        ["tenant_id", "transaction_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_transaction_split_idempotency",
        "transaction_splits",
        type_="unique",
    )
    op.drop_column("transaction_splits", "idempotency_key")
