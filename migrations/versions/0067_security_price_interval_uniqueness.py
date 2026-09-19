"""Include candle interval in security-price deduplication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_security_prices_ts_source",
        "security_prices",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_security_prices_ts_source",
        "security_prices",
        ["security_id", "timestamp", "source", "interval"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_security_prices_ts_source",
        "security_prices",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_security_prices_ts_source",
        "security_prices",
        ["security_id", "timestamp", "source"],
    )
