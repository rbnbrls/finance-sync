"""Convert Trading212 London GBX portfolio prices from pence to pounds.

Revision ID: 0058
Revises: 0057
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE holdings AS h
            SET price = h.price / 100,
                market_value = h.market_value / 100,
                cost_basis = h.cost_basis / 100
            FROM securities AS s, accounts AS a
            WHERE h.security_id = s.id
              AND h.account_id = a.id
              AND a.provider_key = 'trading212'
              AND lower(s.name) LIKE '%l_eq'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE holdings AS h
            SET price = h.price * 100,
                market_value = h.market_value * 100,
                cost_basis = h.cost_basis * 100
            FROM securities AS s, accounts AS a
            WHERE h.security_id = s.id
              AND h.account_id = a.id
              AND a.provider_key = 'trading212'
              AND lower(s.name) LIKE '%l_eq'
            """
        )
    )
