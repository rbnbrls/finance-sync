"""Add fx_rates table for exchange rate observations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "base_currency",
            sa.String(3),
            nullable=False,
            index=True,
            comment="ISO-4217 base currency code (e.g. 'EUR')",
        ),
        sa.Column(
            "quote_currency",
            sa.String(3),
            nullable=False,
            index=True,
            comment="ISO-4217 quote currency code (e.g. 'USD')",
        ),
        sa.Column(
            "rate",
            sa.Numeric(24, 12),
            nullable=False,
            comment="Exchange rate (1 base_currency = rate quote_currency)",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="When the rate observation was recorded",
        ),
        sa.Column(
            "source",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'openbb'"),
            comment="Data source identifier (e.g. 'openbb', 'ecb', 'manual')",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "timestamp",
            "source",
            name="uq_fx_rates_currencies_ts_source",
        ),
        comment="Exchange rate observations for multi-currency support",
    )
    op.create_index(
        "ix_fx_rates_base_quote_ts",
        "fx_rates",
        ["base_currency", "quote_currency", sa.text("timestamp DESC")],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_table("fx_rates")
