"""Add the composite index used by latest-price read queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_security_prices_latest_lookup ON security_prices "
        "(security_id, interval, timestamp DESC)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_security_prices_latest_lookup", table_name="security_prices"
    )
