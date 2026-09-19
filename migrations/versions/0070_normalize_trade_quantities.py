"""Normalize canonical trade quantities to positive unit counts."""

from __future__ import annotations

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trade direction lives in transaction_type.  Keep the transaction IDs
    # and provider identity stable while repairing legacy DEGIRO rows.
    op.execute(
        """
        UPDATE transactions
        SET quantity = ABS(quantity)
        WHERE transaction_type IN ('purchase', 'sale')
          AND quantity < 0
        """
    )


def downgrade() -> None:
    # The canonical contract does not preserve the provider's signed
    # quantity convention, so a safe inverse is intentionally unavailable.
    pass
