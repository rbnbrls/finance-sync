"""Add tax_lots table for cost basis tracking and quantity column to transactions.

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
    # ═══════════════════════════════════════════════════════════════════
    # 1. Add quantity column to transactions (nullable, for tax lots)
    # ═══════════════════════════════════════════════════════════════════
    op.add_column(
        "transactions",
        sa.Column(
            "quantity",
            sa.Numeric(24, 8),
            nullable=True,
            comment="Number of units / shares transacted (for purchase/sale)",
        ),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. Create tax_lots table
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "tax_lots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "security_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        # Transaction links
        sa.Column(
            "purchase_transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Transaction that created this lot",
        ),
        sa.Column(
            "sale_transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Transaction that fully or partially closed this lot",
        ),
        # Quantities
        sa.Column(
            "quantity",
            sa.Numeric(24, 8),
            nullable=False,
            comment="Original number of units acquired (positive)",
        ),
        sa.Column(
            "remaining_quantity",
            sa.Numeric(24, 8),
            nullable=False,
            server_default=sa.text("0"),
            comment="Units still held (decreases on partial sales)",
        ),
        # Cost basis
        sa.Column(
            "cost_basis_total",
            sa.Numeric(24, 8),
            nullable=False,
            comment="Total cost of this lot in local currency",
        ),
        sa.Column(
            "cost_basis_per_unit",
            sa.Numeric(24, 8),
            nullable=False,
            comment="Cost per unit = cost_basis_total / quantity",
        ),
        sa.Column(
            "currency_code",
            sa.String(3),
            nullable=False,
            comment="ISO-4217",
        ),
        # Dates
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the lot was acquired (trade / settlement date)",
        ),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the lot was fully closed (null if still open)",
        ),
        # Realised P&L
        sa.Column(
            "realized_pl",
            sa.Numeric(24, 8),
            nullable=True,
            comment="Realised P&L when this lot was closed",
        ),
        sa.Column(
            "realized_pl_currency",
            sa.String(3),
            nullable=True,
            comment="ISO-4217 for realised P&L",
        ),
        # Wash sale fields
        sa.Column(
            "has_wash_sale_adjustment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True if a wash sale adjustment was applied",
        ),
        sa.Column(
            "disallowed_loss",
            sa.Numeric(24, 8),
            nullable=True,
            comment="Loss disallowed due to wash sale rules",
        ),
        sa.Column(
            "wash_sale_adjustment_type",
            sa.String(32),
            nullable=True,
            comment="loss_disallowed or basis_adjusted",
        ),
        # Cost basis method
        sa.Column(
            "cost_basis_method",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'fifo'"),
            comment="fifo / lifo / specific_id",
        ),
        # Timestamps
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
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tax_lots_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_tax_lots_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            name="fk_tax_lots_security_id_securities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_transaction_id"],
            ["transactions.id"],
            name="fk_tax_lots_purchase_txn_id_transactions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sale_transaction_id"],
            ["transactions.id"],
            name="fk_tax_lots_sale_txn_id_transactions",
            ondelete="SET NULL",
        ),
        comment="Tax lot tracking for cost basis and realised P&L",
    )

    # Indexes
    op.create_index(
        "ix_tax_lots_tenant_id", "tax_lots", ["tenant_id"]
    )
    op.create_index(
        "ix_tax_lots_account_id", "tax_lots", ["account_id"]
    )
    op.create_index(
        "ix_tax_lots_security_id", "tax_lots", ["security_id"]
    )
    op.create_index(
        "ix_tax_lots_tenant_open",
        "tax_lots",
        ["tenant_id", "closed_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "ix_tax_lots_acquisition",
        "tax_lots",
        ["tenant_id", "security_id", "acquired_at"],
    )

    # Unique constraint: one lot per (tenant, account, security, purchase_txn, acquired_at)
    op.create_unique_constraint(
        "uq_tax_lots_purchase",
        "tax_lots",
        ["tenant_id", "account_id", "security_id", "purchase_transaction_id", "acquired_at"],
    )


def downgrade() -> None:
    op.drop_table("tax_lots")
    op.drop_column("transactions", "quantity")
