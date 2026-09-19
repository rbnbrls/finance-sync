"""Track the transfer leg that created a tax lot.

Revision ID: 0065
Revises: 0064
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tax_lots",
        sa.Column(
            "transfer_transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_tax_lots_transfer_transaction",
        "tax_lots",
        "transactions",
        ["transfer_transaction_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tax_lots_transfer_transaction_id",
        "tax_lots",
        ["transfer_transaction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tax_lots_transfer_transaction_id", table_name="tax_lots")
    op.drop_constraint(
        "fk_tax_lots_transfer_transaction", "tax_lots", type_="foreignkey"
    )
    op.drop_column("tax_lots", "transfer_transaction_id")
