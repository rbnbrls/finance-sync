"""Store account NAV and distinguish balance currencies.

Revision ID: 0055
Revises: 0054
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("net_asset_value", sa.Numeric(24, 8), nullable=True),
    )
    op.drop_constraint("uq_balances_snapshot", "balances", type_="unique")
    op.create_unique_constraint(
        "uq_balances_snapshot",
        "balances",
        ["account_id", "observed_at", "balance_kind", "currency_code"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_balances_snapshot", "balances", type_="unique")
    op.create_unique_constraint(
        "uq_balances_snapshot",
        "balances",
        ["account_id", "observed_at", "balance_kind"],
    )
    op.drop_column("accounts", "net_asset_value")
