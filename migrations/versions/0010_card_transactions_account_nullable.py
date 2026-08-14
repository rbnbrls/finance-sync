"""Relax card_transactions.account_id to nullable.

Card payments are tied to a card, not a monetary account. The bunq
``card-payment`` resource (and the connector parser) does not expose the
settling monetary account id, so ingestion cannot populate
``card_transactions.account_id`` at sync time. Card identity is preserved
in ``card_id`` / ``card_type`` / ``card_last_four``.

This relaxes the NOT NULL constraint introduced in 0004 so the G-04 sync
job can persist card transactions without forcing a synthetic account
link. The unique constraint
``(tenant_id, provider_key, external_card_transaction_id)`` is untouched,
so idempotent upserts keep working.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow card transactions without a resolved monetary account."""
    op.alter_column(
        "card_transactions",
        "account_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    """Restore NOT NULL (only safe when no rows carry a NULL account)."""
    op.alter_column(
        "card_transactions",
        "account_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
