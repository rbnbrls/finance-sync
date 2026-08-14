"""Phase 3 reconciliation, scheduled payment and card transaction tables.

Creates the four remaining Phase 3 tables that were not already covered
by dedicated revisions in the linear chain:

- reconciliation_runs, reconciliation_results
- scheduled_payments
- card_transactions

(``fundamental_observations`` / ``security_metadata_observations``,
``fx_rates``, and ``tax_lots`` + ``transactions.quantity`` each have their
own dedicated revision — 0004, 0005 and 0007 respectively.)

Revision ID: 0006
Revises: 0005
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
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. reconciliation_runs
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
    # 2. reconciliation_results
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
    # 3. scheduled_payments
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
    # 4. card_transactions
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


def downgrade() -> None:
    """Drop all Phase 3 tables in reverse order."""
    op.drop_table("card_transactions")
    op.drop_table("scheduled_payments")
    op.drop_table("reconciliation_results")
    op.drop_table("reconciliation_runs")
