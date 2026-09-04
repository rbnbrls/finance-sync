"""Store the exchange venue on price observations.

Revision ID: 0059
Revises: 0058
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "security_prices",
        sa.Column("venue", sa.String(length=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("security_prices", "venue")
