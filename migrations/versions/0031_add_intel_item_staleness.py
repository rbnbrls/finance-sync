"""Add freshness/staleness columns to market-intelligence items.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-19

Implements the incremental-ingestion story requirement: previously valid
observations are never deleted when a provider is down; they are marked
stale only when freshness rules require it.  ``stale_after`` records the
freshness deadline (provider max_age after fetch) and ``is_stale`` is the
soft flag; both default to non-stale so existing rows stay valid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_intelligence_items",
        sa.Column(
            "stale_after",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Freshness deadline: when the provider's max_age elapses "
                "after fetched_at, the item may be marked stale."
            ),
        ),
    )
    op.add_column(
        "market_intelligence_items",
        sa.Column(
            "is_stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Soft staleness flag — observations are never deleted, "
                "only marked stale when freshness rules require it."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("market_intelligence_items", "is_stale")
    op.drop_column("market_intelligence_items", "stale_after")
