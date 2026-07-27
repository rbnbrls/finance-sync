"""Consolidated migration for Phase 3 tables.

Combines the following tables into a single linear migration:
- fx_rates (exchange rate observations)
- fundamental_observations, security_metadata_observations
- reconciliation_runs, reconciliation_results
- scheduled_payments, card_transactions
- tax_lots (plus quantity column on transactions)

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
    # 1. Add quantity to transactions (for tax lot linking)
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
    # 2. fundamental_observations — point-in-time fundamental ratios
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "fundamental_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            comment="FK to securities.id",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="When the fundamental observation was recorded",
        ),
        # Valuation ratios
        sa.Column(
            "pe_ratio",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Price-to-Earnings ratio (TTM)",
        ),
        sa.Column(
            "forward_pe",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Forward Price-to-Earnings ratio",
        ),
        sa.Column(
            "peg_ratio",
            sa.Numeric(20, 6),
            nullable=True,
            comment="PE / Growth ratio",
        ),
        # Per-share metrics
        sa.Column(
            "eps",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Earnings Per Share (TTM)",
        ),
        sa.Column(
            "eps_forward",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Forward EPS estimate",
        ),
        sa.Column(
            "book_value_per_share",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Book Value Per Share",
        ),
        # Dividend
        sa.Column(
            "dividend_yield",
            sa.Numeric(20, 8),
            nullable=True,
            comment="Dividend yield as decimal (e.g. 0.035 = 3.5%)",
        ),
        sa.Column(
            "dividend_rate",
            sa.Numeric(20, 6),
            nullable=True,
            comment="Annual dividend rate per share",
        ),
        # Size & liquidity
        sa.Column(
            "market_cap",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Market capitalisation in base currency",
        ),
        sa.Column(
            "enterprise_value",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Enterprise value",
        ),
        sa.Column(
            "shares_outstanding",
            sa.Numeric(30, 6),
            nullable=True,
            comment="Number of shares outstanding",
        ),
        # Risk & volatility
        sa.Column(
            "beta",
            sa.Numeric(10, 6),
            nullable=True,
            comment="Beta (5-year monthly, vs benchmark)",
        ),
        # 52-week range
        sa.Column(
            "high_52w",
            sa.Numeric(20, 6),
            nullable=True,
            comment="52-week high price",
        ),
        sa.Column(
            "low_52w",
            sa.Numeric(20, 6),
            nullable=True,
            comment="52-week low price",
        ),
        # Metadata
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Data source identifier (e.g. 'openbb', 'manual')",
        ),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB,
            nullable=True,
            comment="Provider-specific additional metadata",
        ),
        # TimestampMixin
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
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
            name="fk_fundamental_observations_security_id_securities",
        ),
        sa.UniqueConstraint(
            "security_id",
            "timestamp",
            "source",
            name="uq_fundamental_obs_ts_source",
        ),
        comment="Point-in-time fundamental metric observations for securities",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 3. security_metadata_observations — structured metadata payloads
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "security_metadata_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "security_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            index=True,
            comment="FK to securities.id",
        ),
        sa.Column(
            "metadata_type",
            sa.String(64),
            nullable=False,
            index=True,
            comment="Discriminator: etf_composition, sector_exposure, "
            "fundamental_ratios, company_profile",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            comment="When the metadata observation was recorded",
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Arbitrary structured metadata payload",
        ),
        sa.Column(
            "label",
            sa.String(256),
            nullable=True,
            comment="Human-readable label (e.g. ETF name, sector title)",
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Data source identifier (e.g. 'openbb', 'manual')",
        ),
        # TimestampMixin
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
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
            name="fk_sec_metadata_obs_security_id_securities",
        ),
        sa.UniqueConstraint(
            "security_id",
            "metadata_type",
            "timestamp",
            "source",
            name="uq_sec_metadata_obs_type_ts_source",
        ),
        comment="Point-in-time structured metadata observations for securities",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 4. fx_rates — exchange rate observations
    # ═══════════════════════════════════════════════════════════════════
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

    # ═══════════════════════════════════════════════════════════════════
    # 5. reconciliation_runs
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "reconciliation_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'running'"),
            comment="'running', 'completed', 'failed', 'cancelled'",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "scope",
            postgresql.JSONB,
            nullable=True,
            comment=(
                "Run scope: {account_ids: [..], date_from: '..', date_to: '..'}"
            ),
        ),
        sa.Column(
            "finding_count",
            sa.Integer,
            nullable=True,
            comment="Total number of findings in this run",
        ),
        sa.Column(
            "summary",
            postgresql.JSONB,
            nullable=True,
            comment=(
                "Summary stats: {duplicates: N, missing: N, "
                "cross_connector: N, by_severity: {info: N, ...}}"
            ),
        ),
        sa.Column(
            "error_message",
            sa.Text,
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 6. reconciliation_results
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "reconciliation_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment=(
                "'duplicate_transaction', 'missing_transaction', "
                "'cross_connector_mismatch', 'amount_mismatch'"
            ),
        ),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'warning'"),
            comment="'info', 'warning', 'error'",
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=True,
            comment="Primary connector involved",
        ),
        sa.Column(
            "other_provider_key",
            sa.String(64),
            nullable=True,
            comment="Secondary connector (cross-connector context)",
        ),
        sa.Column(
            "transaction_id_a",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="First (or only) transaction involved",
        ),
        sa.Column(
            "transaction_id_b",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Second transaction (for duplicates/mismatches)",
        ),
        sa.Column(
            "external_transaction_id_a",
            sa.String(256),
            nullable=True,
        ),
        sa.Column(
            "external_transaction_id_b",
            sa.String(256),
            nullable=True,
        ),
        sa.Column(
            "amount",
            sa.Numeric(24, 8),
            nullable=True,
        ),
        sa.Column(
            "other_amount",
            sa.Numeric(24, 8),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "description",
            sa.String(512),
            nullable=True,
        ),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=True,
            comment="Extra context (score, diff, etc.)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Individual reconciliation findings per run",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 7. scheduled_payments
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "scheduled_payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=False,
            comment="Ingestion connector name",
        ),
        sa.Column(
            "external_schedule_id",
            sa.String(256),
            nullable=False,
            comment="Provider's schedule identifier",
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "amount",
            sa.Numeric(24, 8),
            nullable=False,
            comment="Signed amount (positive = inflow, negative = outflow)",
        ),
        sa.Column(
            "currency_code",
            sa.String(3),
            nullable=False,
            comment="ISO-4217",
        ),
        sa.Column(
            "amount_in_base",
            sa.Numeric(24, 8),
            nullable=True,
            comment="Amount in tenant base currency",
        ),
        sa.Column(
            "frequency",
            sa.String(32),
            nullable=False,
            comment="Recurrence frequency",
        ),
        sa.Column(
            "interval",
            sa.Integer,
            nullable=True,
            comment="Every N units of frequency",
        ),
        sa.Column(
            "next_execution_date",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Next scheduled execution date",
        ),
        sa.Column(
            "end_date",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Recurrence end date",
        ),
        sa.Column(
            "max_executions",
            sa.Integer,
            nullable=True,
            comment="Maximum number of executions",
        ),
        sa.Column(
            "execution_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
            comment="Number of times executed",
        ),
        sa.Column(
            "counterparty_name",
            sa.String(256),
            nullable=True,
            comment="Counterparty / merchant name",
        ),
        sa.Column(
            "counterparty_iban",
            sa.String(34),
            nullable=True,
            comment="Counterparty IBAN",
        ),
        sa.Column(
            "description",
            sa.String(1024),
            nullable=True,
            comment="Payment description / reference",
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="active/paused/completed/cancelled/failed",
        ),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB,
            nullable=True,
            comment="Provider-specific attributes",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_schedule_id",
            name="uq_scheduled_payments_provider",
        ),
        comment="Scheduled / recurring payment templates",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 8. card_transactions
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "card_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "provider_key",
            sa.String(64),
            nullable=False,
            comment="Ingestion connector name",
        ),
        sa.Column(
            "external_card_transaction_id",
            sa.String(256),
            nullable=False,
            comment="Provider's card transaction identifier",
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "amount",
            sa.Numeric(24, 8),
            nullable=False,
            comment="Signed amount (positive = inflow, negative = outflow)",
        ),
        sa.Column(
            "currency_code",
            sa.String(3),
            nullable=False,
            comment="ISO-4217",
        ),
        sa.Column(
            "amount_in_base",
            sa.Numeric(24, 8),
            nullable=True,
            comment="Amount in tenant base currency",
        ),
        sa.Column(
            "merchant_name",
            sa.String(256),
            nullable=True,
            comment="Merchant / store name",
        ),
        sa.Column(
            "merchant_city",
            sa.String(128),
            nullable=True,
            comment="Merchant city",
        ),
        sa.Column(
            "merchant_country",
            sa.String(64),
            nullable=True,
            comment="Merchant country",
        ),
        sa.Column(
            "mcc",
            sa.String(4),
            nullable=True,
            comment="Merchant Category Code",
        ),
        sa.Column(
            "card_id",
            sa.String(256),
            nullable=True,
            comment="Provider card identifier",
        ),
        sa.Column(
            "card_type",
            sa.String(32),
            nullable=True,
            comment="debit/credit/prepaid/virtual",
        ),
        sa.Column(
            "card_last_four",
            sa.String(4),
            nullable=True,
            comment="Last four digits of card PAN",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the transaction occurred (provider time)",
        ),
        sa.Column(
            "booked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the transaction settled / was booked",
        ),
        sa.Column(
            "transaction_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'card_payment'"),
            comment="card_payment / refund / fee / withdrawal / other",
        ),
        sa.Column(
            "authorization_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'authorization'"),
            comment="authorization/settlement/refund/chargeback/other",
        ),
        sa.Column(
            "description",
            sa.String(1024),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="pending / booked / reversed / cancelled",
        ),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB,
            nullable=True,
            comment="Provider-specific attributes",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_card_transaction_id",
            name="uq_card_transactions_provider",
        ),
        comment="Debit/credit card payment transactions",
    )

    # ═══════════════════════════════════════════════════════════════════
    # 9. tax_lots
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
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
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

    # Indexes for tax_lots
    op.create_index("ix_tax_lots_tenant_id", "tax_lots", ["tenant_id"])
    op.create_index("ix_tax_lots_account_id", "tax_lots", ["account_id"])
    op.create_index("ix_tax_lots_security_id", "tax_lots", ["security_id"])
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

    # Unique constraint: one lot per (tenant, account, security, purchase_txn, acquired_at)  # noqa: E501
    op.create_unique_constraint(
        "uq_tax_lots_purchase",
        "tax_lots",
        [
            "tenant_id",
            "account_id",
            "security_id",
            "purchase_transaction_id",
            "acquired_at",
        ],
    )


def downgrade() -> None:
    """Drop all Phase 3 tables and columns in reverse order."""
    op.drop_table("tax_lots")
    op.drop_table("card_transactions")
    op.drop_table("scheduled_payments")
    op.drop_table("reconciliation_results")
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_fx_rates_base_quote_ts", table_name="fx_rates")
    op.drop_table("fx_rates")
    op.drop_table("security_metadata_observations")
    op.drop_table("fundamental_observations")
    op.drop_column("transactions", "quantity")
