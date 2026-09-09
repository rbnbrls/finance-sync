"""Normalize known Trading212 internal instrument identifiers.

Revision ID: 0057
Revises: 0056
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Limit the repair to securities actually used by Trading212 holdings;
    # the same ticker may legitimately exist under another provider.
    op.execute(
        sa.text(
            """
            UPDATE securities AS s
            SET ticker = 'BESI:XAMS',
                name = 'BE Semiconductor Industries'
            WHERE upper(s.ticker) = 'BESIA_EQ'
              AND EXISTS (
                  SELECT 1
                  FROM holdings AS h
                  JOIN accounts AS a ON a.id = h.account_id
                  WHERE h.security_id = s.id
                    AND a.provider_key = 'trading212'
              )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE securities AS s
            SET ticker = 'BESIa_EQ',
                name = 'BESIa_EQ'
            WHERE s.ticker = 'BESI:XAMS'
              AND s.name = 'BE Semiconductor Industries'
            """
        )
    )
