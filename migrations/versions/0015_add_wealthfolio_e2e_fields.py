"""Add Wealthfolio account mapping and exact trade economics.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("unit_price", sa.Numeric(24, 8), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("fee_amount", sa.Numeric(24, 8), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("fee_currency_code", sa.String(3), nullable=True),
    )
    op.create_table(
        "wealthfolio_account_mappings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("wf_account_name", sa.String(256), nullable=False),
        sa.Column("wf_account_id", sa.String(64), nullable=True),
        sa.Column("provider_account_id", sa.String(256), nullable=True),
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
            "tenant_id",
            "account_id",
            name="uq_wealthfolio_mapping_account",
        ),
    )
    op.create_index(
        "ix_wealthfolio_account_mappings_tenant_id",
        "wealthfolio_account_mappings",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wealthfolio_account_mappings_tenant_id",
        table_name="wealthfolio_account_mappings",
    )
    op.drop_table("wealthfolio_account_mappings")
    op.drop_column("transactions", "fee_currency_code")
    op.drop_column("transactions", "fee_amount")
    op.drop_column("transactions", "unit_price")
