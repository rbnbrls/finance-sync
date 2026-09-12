"""Preserve provider-neutral spending metadata on card transactions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_fields = (
    ("provider_metadata_contract", postgresql.JSONB()),
    ("merchant_id", sa.String(256)),
    ("merchant_category_code", sa.String(8)),
    ("original_status", sa.String(64)),
    ("authorization_status", sa.String(32)),
    ("settlement_status", sa.String(32)),
    ("source_record_hash", sa.String(128)),
    ("refund_amount", sa.Numeric(24, 8)),
    ("refund_currency_code", sa.String(3)),
)


def upgrade() -> None:
    for name, column_type in _fields:
        op.add_column(
            "card_transactions", sa.Column(name, column_type, nullable=True)
        )


def downgrade() -> None:
    for name, _ in reversed(_fields):
        op.drop_column("card_transactions", name)
