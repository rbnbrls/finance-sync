"""Initial canonical schema.

Creates all core financial tables: tenants, users, accounts, securities,
security_listings, transactions, holdings, balances, outbox_messages,
and sync_runs.

Revision ID: 0001
Revises: None
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enable pgcrypto for gen_random_uuid() ────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ═══════════════════════════════════════════════════════════════════
    # 1. tenants
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(128), nullable=False, index=True),
        sa.Column("name", sa.String(256), nullable=False),
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
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 2. users
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "role", sa.String(32), nullable=False, server_default="viewer"
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_users_tenant_id_tenants",
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ═══════════════════════════════════════════════════════════════════
    # 3. securities
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "securities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("isin", sa.String(12), nullable=True, index=True),
        sa.Column("figi", sa.String(12), nullable=True),
        sa.Column("cusip", sa.String(9), nullable=True),
        sa.Column("ticker", sa.String(32), nullable=True, index=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("security_type", sa.String(64), nullable=False),
        sa.Column(
            "currency_code", sa.String(3), nullable=False, server_default="EUR"
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
        sa.UniqueConstraint("isin", name="uq_securities_isin"),
        sa.UniqueConstraint("figi", name="uq_securities_figi"),
        sa.UniqueConstraint("cusip", name="uq_securities_cusip"),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 4. accounts
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("external_account_id", sa.String(256), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("account_type", sa.String(64), nullable=False),
        sa.Column("account_subtype", sa.String(64), nullable=True),
        sa.Column(
            "currency_code", sa.String(3), nullable=False, server_default="EUR"
        ),
        sa.Column("current_balance", sa.Numeric(24, 8), nullable=True),
        sa.Column("available_balance", sa.Numeric(24, 8), nullable=True),
        sa.Column("iso_currency_code", sa.String(3), nullable=True),
        sa.Column(
            "provider_metadata",
            postgresql.JSONB,
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_accounts_tenant_id_tenants",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_account_id",
            name="uq_accounts_provider",
        ),
    )
    op.create_index("ix_accounts_tenant_id", "accounts", ["tenant_id"])

    # ═══════════════════════════════════════════════════════════════════
    # 5. security_listings
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "security_listings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mic", sa.String(4), nullable=False),
        sa.Column("exchange_name", sa.String(128), nullable=True),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column(
            "is_primary_listing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
            name="fk_listings_security_id_securities",
        ),
        sa.UniqueConstraint(
            "security_id",
            "mic",
            "currency_code",
            name="uq_listings_venue_ccy",
        ),
    )
    op.create_index(
        "ix_security_listings_security_id", "security_listings", ["security_id"]
    )

    # ═══════════════════════════════════════════════════════════════════
    # 6. transactions
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("external_transaction_id", sa.String(256), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("amount_in_base", sa.Numeric(24, 8), nullable=True),
        sa.Column("base_currency_code", sa.String(3), nullable=True),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transaction_type", sa.String(64), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="pending"
        ),
        sa.Column("provider_fingerprint", sa.String(128), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_transactions_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="RESTRICT",
            name="fk_transactions_account_id_accounts",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="RESTRICT",
            name="fk_transactions_security_id_securities",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_transaction_id",
            name="uq_transactions_provider",
        ),
    )
    op.create_index("ix_transactions_tenant_id", "transactions", ["tenant_id"])
    op.create_index(
        "ix_transactions_account_id", "transactions", ["account_id"]
    )
    op.create_index(
        "ix_transactions_security_id", "transactions", ["security_id"]
    )

    # ═══════════════════════════════════════════════════════════════════
    # 7. holdings
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "holdings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(24, 8), nullable=True),
        sa.Column("cost_basis_currency", sa.String(3), nullable=True),
        sa.Column("market_value", sa.Numeric(24, 8), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("price", sa.Numeric(24, 8), nullable=True),
        sa.Column("price_currency", sa.String(3), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
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
            ["tenant_id"],
            ["tenants.id"],
            name="fk_holdings_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="RESTRICT",
            name="fk_holdings_account_id_accounts",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="RESTRICT",
            name="fk_holdings_security_id_securities",
        ),
    )
    op.create_index("ix_holdings_tenant_id", "holdings", ["tenant_id"])
    op.create_index("ix_holdings_account_id", "holdings", ["account_id"])
    op.create_index("ix_holdings_security_id", "holdings", ["security_id"])

    # ═══════════════════════════════════════════════════════════════════
    # 8. balances
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "balances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("balance_kind", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
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
            ["tenant_id"],
            ["tenants.id"],
            name="fk_balances_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="RESTRICT",
            name="fk_balances_account_id_accounts",
        ),
        sa.UniqueConstraint(
            "account_id",
            "observed_at",
            "balance_kind",
            name="uq_balances_snapshot",
        ),
    )
    op.create_index("ix_balances_tenant_id", "balances", ["tenant_id"])
    op.create_index("ix_balances_account_id", "balances", ["account_id"])

    # ═══════════════════════════════════════════════════════════════════
    # 9. outbox_messages
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "outbox_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_outbox_messages_aggregate_id", "outbox_messages", ["aggregate_id"]
    )
    op.create_index(
        "ix_outbox_messages_status_created",
        "outbox_messages",
        ["status", sa.text("created_at ASC")],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # ═══════════════════════════════════════════════════════════════════
    # 10. sync_runs
    # ═══════════════════════════════════════════════════════════════════
    op.create_table(
        "sync_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="running"
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_sync_runs_connector_status", "sync_runs", ["connector", "status"]
    )


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    op.drop_table("sync_runs")
    op.drop_table("outbox_messages")
    op.drop_table("balances")
    op.drop_table("holdings")
    op.drop_table("transactions")
    op.drop_table("security_listings")
    op.drop_table("accounts")
    op.drop_table("securities")
    op.drop_table("users")
    op.drop_table("tenants")
