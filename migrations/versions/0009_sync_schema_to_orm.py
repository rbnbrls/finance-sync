"""Sync schema to ORM models: create_all-only tables, outbox idempotency key, model alignment.

Brings the database schema in line with the current ORM models, closing the
pre-existing drift reported by ``alembic check`` (create_all-only tables were
never migrated before the lifespan create_all removal in 0008):

- Create ``enrichment_freshness``, ``security_prices``, ``unresolved_securities``,
  ``detected_subscriptions`` and ``resolution_audit_log`` (previously created
  only via ``Base.metadata.create_all``).
- Add ``outbox_messages.idempotency_key`` (unique) so exactly-once delivery
  can be enforced at the schema level.
- Align remaining drift with the ORM: column comments, index definitions,
  column types (``webhook_delivery_logs.webhook_id`` and
  ``reconciliation_results.transaction_id_a/b`` were created as UUID but the
  models declare string columns), and tenant foreign keys (models declare
  ``ForeignKey("tenants.id")`` without ``ondelete``, matching the rest of the
  schema, while phase-3 tables were created with ``ON DELETE CASCADE``).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('enrichment_freshness',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('security_id', sa.UUID(), nullable=False),
    sa.Column('last_metadata_fetch', sa.DateTime(timezone=True), nullable=True, comment='When security metadata was last resolved'),
    sa.Column('last_quote_fetch', sa.DateTime(timezone=True), nullable=True, comment='When the latest quote was last fetched'),
    sa.Column('last_daily_price_fetch', sa.DateTime(timezone=True), nullable=True, comment='When daily historical prices were last synced'),
    sa.Column('last_intraday_price_fetch', sa.DateTime(timezone=True), nullable=True, comment='When intraday prices were last synced'),
    sa.Column('data_source', sa.Text(), nullable=False, comment='Primary data source identifier'),
    sa.Column('status', sa.String(length=32), nullable=False, comment='enrichment_pending/resolved/failed'),
    sa.Column('error_message', sa.Text(), nullable=True, comment='Last error message if enrichment failed'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['security_id'], ['securities.id'], name=op.f('fk_enrichment_freshness_security_id_securities'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_enrichment_freshness'))
    )
    op.create_index(op.f('ix_enrichment_freshness_security_id'), 'enrichment_freshness', ['security_id'], unique=True)
    op.create_table('security_prices',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('security_id', sa.UUID(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False, comment='When the price observation was recorded'),
    sa.Column('price_open', sa.Numeric(), nullable=True, comment='Opening price'),
    sa.Column('price_high', sa.Numeric(), nullable=True, comment='Highest price in the period'),
    sa.Column('price_low', sa.Numeric(), nullable=True, comment='Lowest price in the period'),
    sa.Column('price_close', sa.Numeric(), nullable=True, comment='Closing / last price'),
    sa.Column('volume', sa.Numeric(), nullable=True, comment='Trading volume in base units'),
    sa.Column('source', sa.Text(), nullable=False, comment="Data source identifier (e.g. 'openbb', 'manual')"),
    sa.Column('interval', sa.String(length=16), nullable=False, comment="Candle interval: '1m', '5m', '1h', '1d', etc."),
    sa.Column('currency_code', sa.String(length=3), nullable=False, comment='ISO-4217 currency code'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['security_id'], ['securities.id'], name=op.f('fk_security_prices_security_id_securities'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_security_prices')),
    sa.UniqueConstraint('security_id', 'timestamp', 'source', name='uq_security_prices_ts_source')
    )
    op.create_index(op.f('ix_security_prices_security_id'), 'security_prices', ['security_id'], unique=False)
    op.create_index(op.f('ix_security_prices_timestamp'), 'security_prices', ['timestamp'], unique=False)
    op.create_table('unresolved_securities',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider_key', sa.String(length=64), nullable=False, comment='Connector name'),
    sa.Column('external_security_id', sa.String(length=256), nullable=False, comment='Provider-local security / instrument ID'),
    sa.Column('raw_isin', sa.String(length=12), nullable=True, comment='ISIN as provided by connector'),
    sa.Column('raw_figi', sa.String(length=16), nullable=True, comment='FIGI / FIGI-like code as provided'),
    sa.Column('raw_ticker', sa.String(length=64), nullable=True, comment='Ticker / symbol as provided'),
    sa.Column('raw_name', sa.Text(), nullable=True, comment='Instrument name / description'),
    sa.Column('raw_currency_code', sa.String(length=3), nullable=True, comment='Currency code as provided'),
    sa.Column('raw_metadata', sa.Text(), nullable=True, comment='JSON-encoded provider-specific metadata'),
    sa.Column('resolved_security_id', sa.UUID(), nullable=True, comment='Canonical Security this was mapped to (null = still unresolved)'),
    sa.Column('resolution_method', sa.String(length=32), nullable=True, comment='How it was resolved: auto_isin / auto_figi / auto_ticker / fuzzy_name / manual'),
    sa.Column('resolution_notes', sa.Text(), nullable=True, comment='Human notes from manual resolution'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['resolved_security_id'], ['securities.id'], name=op.f('fk_unresolved_securities_resolved_security_id_securities'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unresolved_securities')),
    sa.UniqueConstraint('provider_key', 'external_security_id', name='uq_unresolved_provider_ext_id')
    )
    op.create_index(op.f('ix_unresolved_securities_provider_key'), 'unresolved_securities', ['provider_key'], unique=False)
    op.create_index(op.f('ix_unresolved_securities_resolved_security_id'), 'unresolved_securities', ['resolved_security_id'], unique=False)
    op.create_table('detected_subscriptions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('merchant_name', sa.String(length=512), nullable=False, comment='Normalised merchant or counterparty name'),
    sa.Column('raw_description', sa.String(length=1024), nullable=True, comment='Most recent raw transaction description'),
    sa.Column('amount', sa.Numeric(precision=24, scale=8), nullable=False, comment='Typical subscription amount (negative = outgoing payment)'),
    sa.Column('currency_code', sa.String(length=3), nullable=False, comment='ISO-4217 currency code'),
    sa.Column('frequency_days', sa.Integer(), nullable=True, comment='Detected interval in calendar days between charges'),
    sa.Column('frequency_label', sa.String(length=32), nullable=True, comment='Human-readable frequency: monthly, weekly, yearly, etc.'),
    sa.Column('confidence', sa.String(length=16), nullable=False, comment="'high', 'medium', or 'low'"),
    sa.Column('detection_method', sa.String(length=32), nullable=False, comment='How the subscription was detected'),
    sa.Column('status', sa.String(length=16), nullable=False, comment="'active', 'paused', 'cancelled', 'ignored', 'unknown'"),
    sa.Column('transaction_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='IDs of the matched transactions'),
    sa.Column('account_id', sa.UUID(), nullable=True, comment='Primary account for this subscription'),
    sa.Column('provider_key', sa.String(length=64), nullable=True, comment='Primary connector provider'),
    sa.Column('security_id', sa.UUID(), nullable=True, comment='Linked security (if merchant is a listed company)'),
    sa.Column('sector', sa.String(length=128), nullable=True, comment='Merchant sector classification from fundamentals data'),
    sa.Column('category', sa.String(length=64), nullable=True, comment='Subscription category: streaming, software, utilities, etc.'),
    sa.Column('first_detected_at', sa.DateTime(timezone=True), nullable=False, comment='Earliest matched transaction date'),
    sa.Column('last_detected_at', sa.DateTime(timezone=True), nullable=False, comment='Most recent matched transaction date'),
    sa.Column('occurrence_count', sa.Integer(), nullable=False, comment='Number of matched occurrences'),
    sa.Column('detection_score', sa.Float(), nullable=True, comment='Algorithmic confidence score (0.0-1.0)'),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Extra detection context (intervals, amount variance, etc.)'),
    sa.Column('user_notes', sa.String(length=1024), nullable=True, comment='User-provided notes or label override'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name=op.f('fk_detected_subscriptions_account_id_accounts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['security_id'], ['securities.id'], name=op.f('fk_detected_subscriptions_security_id_securities'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_detected_subscriptions_tenant_id_tenants')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_detected_subscriptions')),
    comment='Recurring transaction patterns identified as subscriptions'
    )
    op.create_index(op.f('ix_detected_subscriptions_account_id'), 'detected_subscriptions', ['account_id'], unique=False)
    op.create_index(op.f('ix_detected_subscriptions_tenant_id'), 'detected_subscriptions', ['tenant_id'], unique=False)
    op.create_table('resolution_audit_log',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('unresolved_security_id', sa.UUID(), nullable=True, comment='The unresolved record (if any) that triggered this'),
    sa.Column('source_security_id', sa.String(length=64), nullable=True, comment='The incoming security identifier (ISIN, ticker, etc.)'),
    sa.Column('target_security_id', sa.UUID(), nullable=False, comment='Canonical Security the source was mapped to'),
    sa.Column('resolution_method', sa.String(length=32), nullable=False, comment='auto_isin / auto_figi / auto_ticker / fuzzy_name / manual'),
    sa.Column('confidence', sa.String(length=16), nullable=False, comment='exact / high / medium / low'),
    sa.Column('resolver_principal', sa.String(length=128), nullable=False, comment="Who/what performed the resolution: 'system' or user ID"),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=False, comment='When the resolution decision was made'),
    sa.Column('resolution_detail', sa.Text(), nullable=True, comment='Human-readable explanation of how the decision was reached'),
    sa.Column('match_score', sa.Float(), nullable=True, comment='Match confidence score (0.0-1.0) for fuzzy matches'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['target_security_id'], ['securities.id'], name=op.f('fk_resolution_audit_log_target_security_id_securities'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['unresolved_security_id'], ['unresolved_securities.id'], name=op.f('fk_resolution_audit_log_unresolved_security_id_unresolved_securities'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resolution_audit_log'))
    )
    op.create_index(op.f('ix_resolution_audit_log_target_security_id'), 'resolution_audit_log', ['target_security_id'], unique=False)
    op.create_index(op.f('ix_resolution_audit_log_unresolved_security_id'), 'resolution_audit_log', ['unresolved_security_id'], unique=False)
    op.drop_table_comment(
        'ab_account_mappings',
        existing_comment='Maps a finance-sync account to an Actual Budget account',
        schema=None
    )
    op.alter_column('accounts', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment="e.g. 'plaid', 'teller', 'openbb'",
               existing_nullable=False)
    op.alter_column('accounts', 'external_account_id',
               existing_type=sa.VARCHAR(length=256),
               comment="Provider's account ID",
               existing_nullable=False)
    op.alter_column('accounts', 'name',
               existing_type=sa.VARCHAR(length=256),
               comment='Human-readable account name',
               existing_nullable=False)
    op.alter_column('accounts', 'account_type',
               existing_type=sa.VARCHAR(length=64),
               comment='checking/savings/brokerage/credit/loan/investment',
               existing_nullable=False)
    op.alter_column('accounts', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=False,
               existing_server_default=sa.text("'EUR'::character varying"))
    op.alter_column('accounts', 'iso_currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217 for current balance',
               existing_nullable=True)
    op.alter_column('balances', 'observed_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When this balance was observed',
               existing_nullable=False)
    op.alter_column('balances', 'balance_kind',
               existing_type=sa.VARCHAR(length=32),
               comment="'available', 'booked', 'current', 'limit', 'cash'",
               existing_nullable=False)
    op.alter_column('balances', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Balance amount',
               existing_nullable=False)
    op.alter_column('balances', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('balances', 'source',
               existing_type=sa.VARCHAR(length=64),
               comment="'provider_sync', 'manual_entry', 'computed'",
               existing_nullable=False)
    op.alter_column('card_transactions', 'card_type',
               existing_type=sa.VARCHAR(length=32),
               comment='Card type: debit/credit/prepaid/virtual',
               existing_comment='debit/credit/prepaid/virtual',
               existing_nullable=True)
    op.alter_column('card_transactions', 'occurred_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When the transaction actually occurred (provider time)',
               existing_comment='When the transaction occurred (provider time)',
               existing_nullable=False)
    op.alter_column('card_transactions', 'authorization_type',
               existing_type=sa.VARCHAR(length=32),
               comment='authorization / settlement / refund / chargeback / other',
               existing_comment='authorization/settlement/refund/chargeback/other',
               existing_nullable=False,
               existing_server_default=sa.text("'authorization'::character varying"))
    op.alter_column('card_transactions', 'provider_metadata',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment=None,
               existing_comment='Provider-specific attributes',
               existing_nullable=True)
    op.drop_constraint(op.f('fk_card_transactions_tenant_id_tenants'), 'card_transactions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_card_transactions_tenant_id_tenants'), 'card_transactions', 'tenants', ['tenant_id'], ['id'])
    op.drop_table_comment(
        'card_transactions',
        existing_comment='Debit/credit card payment transactions',
        schema=None
    )
    op.alter_column('credentials', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment="Provider identifier, e.g. 'plaid', 'teller', 'yodlee'",
               existing_comment="Provider identifier, e.g. 'plaid', 'teller'",
               existing_nullable=False)
    op.alter_column('credentials', 'description',
               existing_type=sa.TEXT(),
               comment='Human-readable label for this credential entry',
               existing_comment='Human-readable label',
               existing_nullable=True)
    op.drop_index(op.f('ix_credentials_tenant_provider'), table_name='credentials', postgresql_where='(tenant_id IS NOT NULL)')
    op.drop_table_comment(
        'export_deliveries',
        existing_comment='Idempotency cursor: last successfully exported tx per account',
        schema=None
    )
    op.drop_table_comment(
        'export_runs',
        existing_comment='Tracks a single export run for downstream alerting/dashboards',
        schema=None
    )
    op.alter_column('fundamental_observations', 'security_id',
               existing_type=sa.UUID(),
               comment=None,
               existing_comment='FK to securities.id',
               existing_nullable=False)
    op.alter_column('fundamental_observations', 'pe_ratio',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Price-to-Earnings ratio (trailing twelve months)',
               existing_comment='Price-to-Earnings ratio (TTM)',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'eps',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Earnings Per Share (trailing twelve months)',
               existing_comment='Earnings Per Share (TTM)',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'eps_forward',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Forward Earnings Per Share estimate',
               existing_comment='Forward EPS estimate',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'dividend_yield',
               existing_type=sa.NUMERIC(precision=20, scale=8),
               comment='Dividend yield as a decimal (e.g. 0.035 = 3.5%)',
               existing_comment='Dividend yield as decimal (e.g. 0.035 = 3.5%)',
               existing_nullable=True)
    op.drop_table_comment(
        'fundamental_observations',
        existing_comment='Point-in-time fundamental metric observations for securities',
        schema=None
    )
    op.drop_index(op.f('ix_fx_rates_base_quote_ts'), table_name='fx_rates')
    op.drop_table_comment(
        'fx_rates',
        existing_comment='Exchange rate observations for multi-currency support',
        schema=None
    )
    op.alter_column('holdings', 'observed_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When this snapshot was observed / reported',
               existing_nullable=False)
    op.alter_column('holdings', 'quantity',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Number of units held',
               existing_nullable=False)
    op.alter_column('holdings', 'cost_basis',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Total cost basis',
               existing_nullable=True)
    op.alter_column('holdings', 'cost_basis_currency',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=True)
    op.alter_column('holdings', 'market_value',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Market value at observation time',
               existing_nullable=True)
    op.alter_column('holdings', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217 for quantity/market_value',
               existing_nullable=False)
    op.alter_column('holdings', 'price',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Unit price at observation time',
               existing_nullable=True)
    op.alter_column('holdings', 'price_currency',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217 for price',
               existing_nullable=True)
    op.alter_column('holdings', 'source',
               existing_type=sa.VARCHAR(length=64),
               comment="'provider_sync', 'computed', 'manual_adjustment'",
               existing_nullable=False)
    op.add_column('outbox_messages', sa.Column('idempotency_key', sa.String(length=128), nullable=True, comment='Optional idempotency key for exactly-once delivery'))
    op.alter_column('outbox_messages', 'aggregate_id',
               existing_type=sa.VARCHAR(length=128),
               comment='Aggregate root ID that produced this event',
               existing_nullable=False)
    op.alter_column('outbox_messages', 'aggregate_type',
               existing_type=sa.VARCHAR(length=64),
               comment="e.g. 'account', 'transaction', 'connection'",
               existing_nullable=False)
    op.alter_column('outbox_messages', 'event_type',
               existing_type=sa.VARCHAR(length=128),
               comment="e.g. 'account.created', 'transaction.booked'",
               existing_nullable=False)
    op.alter_column('outbox_messages', 'payload',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment='Serialised event data',
               existing_nullable=False)
    op.alter_column('outbox_messages', 'status',
               existing_type=sa.VARCHAR(length=16),
               comment="'pending', 'sent', 'failed'",
               existing_nullable=False,
               existing_server_default=sa.text("'pending'::character varying"))
    op.drop_index(op.f('ix_outbox_messages_status_created'), table_name='outbox_messages', postgresql_where="((status)::text = 'pending'::text)")
    op.create_unique_constraint(op.f('uq_outbox_messages_idempotency_key'), 'outbox_messages', ['idempotency_key'])
    op.alter_column('reconciliation_results', 'other_provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment='Secondary connector (for cross-connector)',
               existing_comment='Secondary connector (cross-connector context)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'transaction_id_a',
               existing_type=sa.UUID(),
               type_=sa.String(),
               existing_comment='First (or only) transaction involved',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'transaction_id_b',
               existing_type=sa.UUID(),
               type_=sa.String(),
               existing_comment='Second transaction (for duplicates/mismatches)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'external_transaction_id_a',
               existing_type=sa.VARCHAR(length=256),
               comment='Provider ID of first transaction',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'external_transaction_id_b',
               existing_type=sa.VARCHAR(length=256),
               comment='Provider ID of second transaction',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Transaction amount (if applicable)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'other_amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Other amount for comparison (mismatch context)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'description',
               existing_type=sa.VARCHAR(length=512),
               comment='Human-readable finding summary',
               existing_nullable=True)
    op.drop_constraint(op.f('fk_reconciliation_results_tenant_id_tenants'), 'reconciliation_results', type_='foreignkey')
    op.create_foreign_key(op.f('fk_reconciliation_results_tenant_id_tenants'), 'reconciliation_results', 'tenants', ['tenant_id'], ['id'])
    op.alter_column('reconciliation_runs', 'error_message',
               existing_type=sa.TEXT(),
               type_=sa.String(),
               existing_nullable=True)
    op.drop_constraint(op.f('fk_reconciliation_runs_tenant_id_tenants'), 'reconciliation_runs', type_='foreignkey')
    op.create_foreign_key(op.f('fk_reconciliation_runs_tenant_id_tenants'), 'reconciliation_runs', 'tenants', ['tenant_id'], ['id'])
    op.alter_column('scheduled_payments', 'interval',
               existing_type=sa.INTEGER(),
               comment='Every N units of frequency (e.g. every 2 months)',
               existing_comment='Every N units of frequency',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'end_date',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='Recurrence end date (optional)',
               existing_comment='Recurrence end date',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'max_executions',
               existing_type=sa.INTEGER(),
               comment='Maximum number of executions (optional)',
               existing_comment='Maximum number of executions',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'provider_metadata',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment=None,
               existing_comment='Provider-specific attributes',
               existing_nullable=True)
    op.drop_constraint(op.f('fk_scheduled_payments_tenant_id_tenants'), 'scheduled_payments', type_='foreignkey')
    op.create_foreign_key(op.f('fk_scheduled_payments_tenant_id_tenants'), 'scheduled_payments', 'tenants', ['tenant_id'], ['id'])
    op.drop_table_comment(
        'scheduled_payments',
        existing_comment='Scheduled / recurring payment templates',
        schema=None
    )
    op.alter_column('securities', 'isin',
               existing_type=sa.VARCHAR(length=12),
               comment='ISO 6166 ISIN',
               existing_nullable=True)
    op.alter_column('securities', 'figi',
               existing_type=sa.VARCHAR(length=12),
               comment='OpenFIGI identifier',
               existing_nullable=True)
    op.alter_column('securities', 'cusip',
               existing_type=sa.VARCHAR(length=9),
               comment='CUSIP number (US/CA)',
               existing_nullable=True)
    op.alter_column('securities', 'ticker',
               existing_type=sa.VARCHAR(length=32),
               comment='Popular ticker symbol',
               existing_nullable=True)
    op.alter_column('securities', 'name',
               existing_type=sa.VARCHAR(length=512),
               comment='Canonical instrument name',
               existing_nullable=False)
    op.alter_column('securities', 'security_type',
               existing_type=sa.VARCHAR(length=64),
               comment='stock/etf/mutual_fund/bond/option/crypto/currency/other',
               existing_nullable=False)
    op.alter_column('securities', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=False,
               existing_server_default=sa.text("'EUR'::character varying"))
    op.alter_column('security_listings', 'mic',
               existing_type=sa.VARCHAR(length=4),
               comment="ISO 10383 Market Identifier Code (e.g. 'XAMS', 'XNYS')",
               existing_nullable=False)
    op.alter_column('security_listings', 'ticker',
               existing_type=sa.VARCHAR(length=32),
               comment='Ticker at this venue',
               existing_nullable=False)
    op.alter_column('security_listings', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('security_metadata_observations', 'security_id',
               existing_type=sa.UUID(),
               comment=None,
               existing_comment='FK to securities.id',
               existing_nullable=False)
    op.alter_column('security_metadata_observations', 'label',
               existing_type=sa.VARCHAR(length=256),
               comment='Human-readable label for this observation (e.g. ETF name, sector title)',
               existing_comment='Human-readable label (e.g. ETF name, sector title)',
               existing_nullable=True)
    op.drop_table_comment(
        'security_metadata_observations',
        existing_comment='Point-in-time structured metadata observations for securities',
        schema=None
    )
    op.alter_column('sync_runs', 'connector',
               existing_type=sa.VARCHAR(length=64),
               comment="Connector name, e.g. 'plaid', 'teller', 'openbb'",
               existing_nullable=False)
    op.alter_column('sync_runs', 'status',
               existing_type=sa.VARCHAR(length=16),
               comment="'running', 'completed', 'failed', 'cancelled'",
               existing_nullable=False,
               existing_server_default=sa.text("'running'::character varying"))
    op.drop_index(op.f('ix_sync_runs_connector_status'), table_name='sync_runs')
    op.alter_column('tax_lots', 'remaining_quantity',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Units still held in this lot (decreases on partial sales)',
               existing_comment='Units still held (decreases on partial sales)',
               existing_nullable=False,
               existing_server_default=sa.text('0'))
    op.alter_column('tax_lots', 'cost_basis_total',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Total cost of this lot (local currency)',
               existing_comment='Total cost of this lot in local currency',
               existing_nullable=False)
    op.alter_column('tax_lots', 'realized_pl',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Realised P&L when this lot was closed (proceeds - cost)',
               existing_comment='Realised P&L when this lot was closed',
               existing_nullable=True)
    op.alter_column('tax_lots', 'has_wash_sale_adjustment',
               existing_type=sa.BOOLEAN(),
               comment='True if a wash sale adjustment was applied to this lot',
               existing_comment='True if a wash sale adjustment was applied',
               existing_nullable=False,
               existing_server_default=sa.text('false'))
    op.alter_column('tax_lots', 'cost_basis_method',
               existing_type=sa.VARCHAR(length=16),
               comment='Method used for cost basis (fifo / lifo / specific_id)',
               existing_comment='fifo / lifo / specific_id',
               existing_nullable=False,
               existing_server_default=sa.text("'fifo'::character varying"))
    op.drop_index(op.f('ix_tax_lots_acquisition'), table_name='tax_lots')
    op.drop_index(op.f('ix_tax_lots_tenant_open'), table_name='tax_lots', postgresql_where='(closed_at IS NULL)')
    op.drop_table_comment(
        'tax_lots',
        existing_comment='Tax lot tracking for cost basis and realised P&L',
        schema=None
    )
    op.alter_column('transactions', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment='Ingestion connector name',
               existing_nullable=False)
    op.alter_column('transactions', 'external_transaction_id',
               existing_type=sa.VARCHAR(length=256),
               comment="Provider's transaction ID",
               existing_nullable=False)
    op.alter_column('transactions', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Signed amount (positive = inflow, negative = outflow)',
               existing_nullable=False)
    op.alter_column('transactions', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('transactions', 'amount_in_base',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Amount in tenant base currency',
               existing_nullable=True)
    op.alter_column('transactions', 'base_currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment='ISO-4217 for amount_in_base',
               existing_nullable=True)
    op.alter_column('transactions', 'fx_rate',
               existing_type=sa.NUMERIC(precision=18, scale=8),
               comment='FX rate used for conversion',
               existing_nullable=True)
    op.alter_column('transactions', 'occurred_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When the transaction actually occurred (provider time)',
               existing_nullable=False)
    op.alter_column('transactions', 'booked_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When the provider booked / settled the transaction',
               existing_nullable=True)
    op.alter_column('transactions', 'transaction_type',
               existing_type=sa.VARCHAR(length=64),
               comment='transfer/payment/purchase/sale/fee/interest/dividend/withdrawal/deposit/other',
               existing_nullable=False)
    op.alter_column('transactions', 'status',
               existing_type=sa.VARCHAR(length=32),
               comment="'pending', 'booked', 'reversed', 'cancelled'",
               existing_nullable=False,
               existing_server_default=sa.text("'pending'::character varying"))
    op.alter_column('transactions', 'provider_fingerprint',
               existing_type=sa.VARCHAR(length=128),
               comment='Provider-side checksum / hash',
               existing_nullable=True)
    op.alter_column('webhook_delivery_logs', 'webhook_id',
               existing_type=sa.UUID(),
               type_=sa.String(length=36),
               comment='FK to webhooks.id (no actual FK constraint for audit safety)',
               existing_comment='FK to webhooks.id (audit safety, no actual FK)',
               existing_nullable=False)
    op.alter_column('webhook_delivery_logs', 'next_retry_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When to retry next (null if max attempts reached or delivered)',
               existing_comment='Next retry time (null if max reached or delivered)',
               existing_nullable=True)
    op.drop_index(op.f('ix_webhook_delivery_logs_status_retry'), table_name='webhook_delivery_logs', postgresql_where="(((status)::text = 'failed'::text) AND (next_retry_at IS NOT NULL))")
    op.create_index(op.f('ix_webhook_delivery_logs_tenant_id'), 'webhook_delivery_logs', ['tenant_id'], unique=False)
    op.drop_table_comment(
        'webhook_delivery_logs',
        existing_comment='Audit log of webhook delivery attempts',
        schema=None
    )
    op.alter_column('webhooks', 'events',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment="List of event types this webhook subscribes to, e.g. ['sync.completed']",
               existing_comment='List of subscribed event types',
               existing_nullable=False,
               existing_server_default=sa.text("'[]'::jsonb"))
    op.alter_column('webhooks', 'rate_limit_max_per_minute',
               existing_type=sa.INTEGER(),
               comment='Max deliveries allowed per 60-second sliding window',
               existing_comment='Max deliveries per 60-second sliding window',
               existing_nullable=False,
               existing_server_default=sa.text('60'))
    op.drop_index(op.f('ix_webhooks_events_gin'), table_name='webhooks', postgresql_using='gin')
    op.drop_index(op.f('ix_webhooks_tenant_active'), table_name='webhooks', postgresql_where='(is_active IS TRUE)')
    op.create_index(op.f('ix_webhooks_tenant_id'), 'webhooks', ['tenant_id'], unique=False)
    op.drop_constraint(op.f('fk_webhooks_tenant_id_tenants'), 'webhooks', type_='foreignkey')
    op.drop_table_comment(
        'webhooks',
        existing_comment='Registered webhook endpoints for event notifications',
        schema=None
    )


def downgrade() -> None:
    op.create_table_comment(
        'webhooks',
        'Registered webhook endpoints for event notifications',
        existing_comment=None,
        schema=None
    )
    op.create_foreign_key(op.f('fk_webhooks_tenant_id_tenants'), 'webhooks', 'tenants', ['tenant_id'], ['id'])
    op.drop_index(op.f('ix_webhooks_tenant_id'), table_name='webhooks')
    op.create_index(op.f('ix_webhooks_tenant_active'), 'webhooks', ['tenant_id', 'is_active'], unique=False, postgresql_where='(is_active IS TRUE)')
    op.create_index(op.f('ix_webhooks_events_gin'), 'webhooks', ['events'], unique=False, postgresql_using='gin')
    op.alter_column('webhooks', 'rate_limit_max_per_minute',
               existing_type=sa.INTEGER(),
               comment='Max deliveries per 60-second sliding window',
               existing_comment='Max deliveries allowed per 60-second sliding window',
               existing_nullable=False,
               existing_server_default=sa.text('60'))
    op.alter_column('webhooks', 'events',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment='List of subscribed event types',
               existing_comment="List of event types this webhook subscribes to, e.g. ['sync.completed']",
               existing_nullable=False,
               existing_server_default=sa.text("'[]'::jsonb"))
    op.create_table_comment(
        'webhook_delivery_logs',
        'Audit log of webhook delivery attempts',
        existing_comment=None,
        schema=None
    )
    op.drop_index(op.f('ix_webhook_delivery_logs_tenant_id'), table_name='webhook_delivery_logs')
    op.create_index(op.f('ix_webhook_delivery_logs_status_retry'), 'webhook_delivery_logs', ['status', 'next_retry_at'], unique=False, postgresql_where="(((status)::text = 'failed'::text) AND (next_retry_at IS NOT NULL))")
    op.alter_column('webhook_delivery_logs', 'next_retry_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='Next retry time (null if max reached or delivered)',
               existing_comment='When to retry next (null if max attempts reached or delivered)',
               existing_nullable=True)
    op.alter_column('webhook_delivery_logs', 'webhook_id',
               existing_type=sa.String(length=36),
               type_=sa.UUID(),
               postgresql_using='webhook_id::uuid',
               comment='FK to webhooks.id (audit safety, no actual FK)',
               existing_comment='FK to webhooks.id (no actual FK constraint for audit safety)',
               existing_nullable=False)
    op.alter_column('transactions', 'provider_fingerprint',
               existing_type=sa.VARCHAR(length=128),
               comment=None,
               existing_comment='Provider-side checksum / hash',
               existing_nullable=True)
    op.alter_column('transactions', 'status',
               existing_type=sa.VARCHAR(length=32),
               comment=None,
               existing_comment="'pending', 'booked', 'reversed', 'cancelled'",
               existing_nullable=False,
               existing_server_default=sa.text("'pending'::character varying"))
    op.alter_column('transactions', 'transaction_type',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment='transfer/payment/purchase/sale/fee/interest/dividend/withdrawal/deposit/other',
               existing_nullable=False)
    op.alter_column('transactions', 'booked_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='When the provider booked / settled the transaction',
               existing_nullable=True)
    op.alter_column('transactions', 'occurred_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='When the transaction actually occurred (provider time)',
               existing_nullable=False)
    op.alter_column('transactions', 'fx_rate',
               existing_type=sa.NUMERIC(precision=18, scale=8),
               comment=None,
               existing_comment='FX rate used for conversion',
               existing_nullable=True)
    op.alter_column('transactions', 'base_currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217 for amount_in_base',
               existing_nullable=True)
    op.alter_column('transactions', 'amount_in_base',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Amount in tenant base currency',
               existing_nullable=True)
    op.alter_column('transactions', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('transactions', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Signed amount (positive = inflow, negative = outflow)',
               existing_nullable=False)
    op.alter_column('transactions', 'external_transaction_id',
               existing_type=sa.VARCHAR(length=256),
               comment=None,
               existing_comment="Provider's transaction ID",
               existing_nullable=False)
    op.alter_column('transactions', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment='Ingestion connector name',
               existing_nullable=False)
    op.create_table_comment(
        'tax_lots',
        'Tax lot tracking for cost basis and realised P&L',
        existing_comment=None,
        schema=None
    )
    op.create_index(op.f('ix_tax_lots_tenant_open'), 'tax_lots', ['tenant_id', 'closed_at'], unique=False, postgresql_where='(closed_at IS NULL)')
    op.create_index(op.f('ix_tax_lots_acquisition'), 'tax_lots', ['tenant_id', 'security_id', 'acquired_at'], unique=False)
    op.alter_column('tax_lots', 'cost_basis_method',
               existing_type=sa.VARCHAR(length=16),
               comment='fifo / lifo / specific_id',
               existing_comment='Method used for cost basis (fifo / lifo / specific_id)',
               existing_nullable=False,
               existing_server_default=sa.text("'fifo'::character varying"))
    op.alter_column('tax_lots', 'has_wash_sale_adjustment',
               existing_type=sa.BOOLEAN(),
               comment='True if a wash sale adjustment was applied',
               existing_comment='True if a wash sale adjustment was applied to this lot',
               existing_nullable=False,
               existing_server_default=sa.text('false'))
    op.alter_column('tax_lots', 'realized_pl',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Realised P&L when this lot was closed',
               existing_comment='Realised P&L when this lot was closed (proceeds - cost)',
               existing_nullable=True)
    op.alter_column('tax_lots', 'cost_basis_total',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Total cost of this lot in local currency',
               existing_comment='Total cost of this lot (local currency)',
               existing_nullable=False)
    op.alter_column('tax_lots', 'remaining_quantity',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment='Units still held (decreases on partial sales)',
               existing_comment='Units still held in this lot (decreases on partial sales)',
               existing_nullable=False,
               existing_server_default=sa.text('0'))
    op.create_index(op.f('ix_sync_runs_connector_status'), 'sync_runs', ['connector', 'status'], unique=False)
    op.alter_column('sync_runs', 'status',
               existing_type=sa.VARCHAR(length=16),
               comment=None,
               existing_comment="'running', 'completed', 'failed', 'cancelled'",
               existing_nullable=False,
               existing_server_default=sa.text("'running'::character varying"))
    op.alter_column('sync_runs', 'connector',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="Connector name, e.g. 'plaid', 'teller', 'openbb'",
               existing_nullable=False)
    op.create_table_comment(
        'security_metadata_observations',
        'Point-in-time structured metadata observations for securities',
        existing_comment=None,
        schema=None
    )
    op.alter_column('security_metadata_observations', 'label',
               existing_type=sa.VARCHAR(length=256),
               comment='Human-readable label (e.g. ETF name, sector title)',
               existing_comment='Human-readable label for this observation (e.g. ETF name, sector title)',
               existing_nullable=True)
    op.alter_column('security_metadata_observations', 'security_id',
               existing_type=sa.UUID(),
               comment='FK to securities.id',
               existing_nullable=False)
    op.alter_column('security_listings', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('security_listings', 'ticker',
               existing_type=sa.VARCHAR(length=32),
               comment=None,
               existing_comment='Ticker at this venue',
               existing_nullable=False)
    op.alter_column('security_listings', 'mic',
               existing_type=sa.VARCHAR(length=4),
               comment=None,
               existing_comment="ISO 10383 Market Identifier Code (e.g. 'XAMS', 'XNYS')",
               existing_nullable=False)
    op.alter_column('securities', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=False,
               existing_server_default=sa.text("'EUR'::character varying"))
    op.alter_column('securities', 'security_type',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment='stock/etf/mutual_fund/bond/option/crypto/currency/other',
               existing_nullable=False)
    op.alter_column('securities', 'name',
               existing_type=sa.VARCHAR(length=512),
               comment=None,
               existing_comment='Canonical instrument name',
               existing_nullable=False)
    op.alter_column('securities', 'ticker',
               existing_type=sa.VARCHAR(length=32),
               comment=None,
               existing_comment='Popular ticker symbol',
               existing_nullable=True)
    op.alter_column('securities', 'cusip',
               existing_type=sa.VARCHAR(length=9),
               comment=None,
               existing_comment='CUSIP number (US/CA)',
               existing_nullable=True)
    op.alter_column('securities', 'figi',
               existing_type=sa.VARCHAR(length=12),
               comment=None,
               existing_comment='OpenFIGI identifier',
               existing_nullable=True)
    op.alter_column('securities', 'isin',
               existing_type=sa.VARCHAR(length=12),
               comment=None,
               existing_comment='ISO 6166 ISIN',
               existing_nullable=True)
    op.create_table_comment(
        'scheduled_payments',
        'Scheduled / recurring payment templates',
        existing_comment=None,
        schema=None
    )
    op.drop_constraint(op.f('fk_scheduled_payments_tenant_id_tenants'), 'scheduled_payments', type_='foreignkey')
    op.create_foreign_key(op.f('fk_scheduled_payments_tenant_id_tenants'), 'scheduled_payments', 'tenants', ['tenant_id'], ['id'], ondelete='CASCADE')
    op.alter_column('scheduled_payments', 'provider_metadata',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment='Provider-specific attributes',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'max_executions',
               existing_type=sa.INTEGER(),
               comment='Maximum number of executions',
               existing_comment='Maximum number of executions (optional)',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'end_date',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='Recurrence end date',
               existing_comment='Recurrence end date (optional)',
               existing_nullable=True)
    op.alter_column('scheduled_payments', 'interval',
               existing_type=sa.INTEGER(),
               comment='Every N units of frequency',
               existing_comment='Every N units of frequency (e.g. every 2 months)',
               existing_nullable=True)
    op.drop_constraint(op.f('fk_reconciliation_runs_tenant_id_tenants'), 'reconciliation_runs', type_='foreignkey')
    op.create_foreign_key(op.f('fk_reconciliation_runs_tenant_id_tenants'), 'reconciliation_runs', 'tenants', ['tenant_id'], ['id'], ondelete='CASCADE')
    op.alter_column('reconciliation_runs', 'error_message',
               existing_type=sa.String(),
               type_=sa.TEXT(),
               existing_nullable=True)
    op.drop_constraint(op.f('fk_reconciliation_results_tenant_id_tenants'), 'reconciliation_results', type_='foreignkey')
    op.create_foreign_key(op.f('fk_reconciliation_results_tenant_id_tenants'), 'reconciliation_results', 'tenants', ['tenant_id'], ['id'], ondelete='CASCADE')
    op.alter_column('reconciliation_results', 'description',
               existing_type=sa.VARCHAR(length=512),
               comment=None,
               existing_comment='Human-readable finding summary',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'other_amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Other amount for comparison (mismatch context)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Transaction amount (if applicable)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'external_transaction_id_b',
               existing_type=sa.VARCHAR(length=256),
               comment=None,
               existing_comment='Provider ID of second transaction',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'external_transaction_id_a',
               existing_type=sa.VARCHAR(length=256),
               comment=None,
               existing_comment='Provider ID of first transaction',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'transaction_id_b',
               existing_type=sa.String(),
               type_=sa.UUID(),
               postgresql_using='transaction_id_b::uuid',
               existing_comment='Second transaction (for duplicates/mismatches)',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'transaction_id_a',
               existing_type=sa.String(),
               type_=sa.UUID(),
               postgresql_using='transaction_id_a::uuid',
               existing_comment='First (or only) transaction involved',
               existing_nullable=True)
    op.alter_column('reconciliation_results', 'other_provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment='Secondary connector (cross-connector context)',
               existing_comment='Secondary connector (for cross-connector)',
               existing_nullable=True)
    op.drop_constraint(op.f('uq_outbox_messages_idempotency_key'), 'outbox_messages', type_='unique')
    op.create_index(op.f('ix_outbox_messages_status_created'), 'outbox_messages', ['status', 'created_at'], unique=False, postgresql_where="((status)::text = 'pending'::text)")
    op.alter_column('outbox_messages', 'status',
               existing_type=sa.VARCHAR(length=16),
               comment=None,
               existing_comment="'pending', 'sent', 'failed'",
               existing_nullable=False,
               existing_server_default=sa.text("'pending'::character varying"))
    op.alter_column('outbox_messages', 'payload',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment=None,
               existing_comment='Serialised event data',
               existing_nullable=False)
    op.alter_column('outbox_messages', 'event_type',
               existing_type=sa.VARCHAR(length=128),
               comment=None,
               existing_comment="e.g. 'account.created', 'transaction.booked'",
               existing_nullable=False)
    op.alter_column('outbox_messages', 'aggregate_type',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="e.g. 'account', 'transaction', 'connection'",
               existing_nullable=False)
    op.alter_column('outbox_messages', 'aggregate_id',
               existing_type=sa.VARCHAR(length=128),
               comment=None,
               existing_comment='Aggregate root ID that produced this event',
               existing_nullable=False)
    op.drop_column('outbox_messages', 'idempotency_key')
    op.alter_column('holdings', 'source',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="'provider_sync', 'computed', 'manual_adjustment'",
               existing_nullable=False)
    op.alter_column('holdings', 'price_currency',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217 for price',
               existing_nullable=True)
    op.alter_column('holdings', 'price',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Unit price at observation time',
               existing_nullable=True)
    op.alter_column('holdings', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217 for quantity/market_value',
               existing_nullable=False)
    op.alter_column('holdings', 'market_value',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Market value at observation time',
               existing_nullable=True)
    op.alter_column('holdings', 'cost_basis_currency',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=True)
    op.alter_column('holdings', 'cost_basis',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Total cost basis',
               existing_nullable=True)
    op.alter_column('holdings', 'quantity',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Number of units held',
               existing_nullable=False)
    op.alter_column('holdings', 'observed_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='When this snapshot was observed / reported',
               existing_nullable=False)
    op.create_table_comment(
        'fx_rates',
        'Exchange rate observations for multi-currency support',
        existing_comment=None,
        schema=None
    )
    op.create_index(op.f('ix_fx_rates_base_quote_ts'), 'fx_rates', ['base_currency', 'quote_currency', sa.literal_column('timestamp DESC')], unique=False)
    op.create_table_comment(
        'fundamental_observations',
        'Point-in-time fundamental metric observations for securities',
        existing_comment=None,
        schema=None
    )
    op.alter_column('fundamental_observations', 'dividend_yield',
               existing_type=sa.NUMERIC(precision=20, scale=8),
               comment='Dividend yield as decimal (e.g. 0.035 = 3.5%)',
               existing_comment='Dividend yield as a decimal (e.g. 0.035 = 3.5%)',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'eps_forward',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Forward EPS estimate',
               existing_comment='Forward Earnings Per Share estimate',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'eps',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Earnings Per Share (TTM)',
               existing_comment='Earnings Per Share (trailing twelve months)',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'pe_ratio',
               existing_type=sa.NUMERIC(precision=20, scale=6),
               comment='Price-to-Earnings ratio (TTM)',
               existing_comment='Price-to-Earnings ratio (trailing twelve months)',
               existing_nullable=True)
    op.alter_column('fundamental_observations', 'security_id',
               existing_type=sa.UUID(),
               comment='FK to securities.id',
               existing_nullable=False)
    op.create_table_comment(
        'export_runs',
        'Tracks a single export run for downstream alerting/dashboards',
        existing_comment=None,
        schema=None
    )
    op.create_table_comment(
        'export_deliveries',
        'Idempotency cursor: last successfully exported tx per account',
        existing_comment=None,
        schema=None
    )
    op.create_index(op.f('ix_credentials_tenant_provider'), 'credentials', ['tenant_id', 'provider_key'], unique=True, postgresql_where='(tenant_id IS NOT NULL)')
    op.alter_column('credentials', 'description',
               existing_type=sa.TEXT(),
               comment='Human-readable label',
               existing_comment='Human-readable label for this credential entry',
               existing_nullable=True)
    op.alter_column('credentials', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment="Provider identifier, e.g. 'plaid', 'teller'",
               existing_comment="Provider identifier, e.g. 'plaid', 'teller', 'yodlee'",
               existing_nullable=False)
    op.create_table_comment(
        'card_transactions',
        'Debit/credit card payment transactions',
        existing_comment=None,
        schema=None
    )
    op.drop_constraint(op.f('fk_card_transactions_tenant_id_tenants'), 'card_transactions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_card_transactions_tenant_id_tenants'), 'card_transactions', 'tenants', ['tenant_id'], ['id'], ondelete='CASCADE')
    op.alter_column('card_transactions', 'provider_metadata',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               comment='Provider-specific attributes',
               existing_nullable=True)
    op.alter_column('card_transactions', 'authorization_type',
               existing_type=sa.VARCHAR(length=32),
               comment='authorization/settlement/refund/chargeback/other',
               existing_comment='authorization / settlement / refund / chargeback / other',
               existing_nullable=False,
               existing_server_default=sa.text("'authorization'::character varying"))
    op.alter_column('card_transactions', 'occurred_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='When the transaction occurred (provider time)',
               existing_comment='When the transaction actually occurred (provider time)',
               existing_nullable=False)
    op.alter_column('card_transactions', 'card_type',
               existing_type=sa.VARCHAR(length=32),
               comment='debit/credit/prepaid/virtual',
               existing_comment='Card type: debit/credit/prepaid/virtual',
               existing_nullable=True)
    op.alter_column('balances', 'source',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="'provider_sync', 'manual_entry', 'computed'",
               existing_nullable=False)
    op.alter_column('balances', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=False)
    op.alter_column('balances', 'amount',
               existing_type=sa.NUMERIC(precision=24, scale=8),
               comment=None,
               existing_comment='Balance amount',
               existing_nullable=False)
    op.alter_column('balances', 'balance_kind',
               existing_type=sa.VARCHAR(length=32),
               comment=None,
               existing_comment="'available', 'booked', 'current', 'limit', 'cash'",
               existing_nullable=False)
    op.alter_column('balances', 'observed_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='When this balance was observed',
               existing_nullable=False)
    op.alter_column('accounts', 'iso_currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217 for current balance',
               existing_nullable=True)
    op.alter_column('accounts', 'currency_code',
               existing_type=sa.VARCHAR(length=3),
               comment=None,
               existing_comment='ISO-4217',
               existing_nullable=False,
               existing_server_default=sa.text("'EUR'::character varying"))
    op.alter_column('accounts', 'account_type',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment='checking/savings/brokerage/credit/loan/investment',
               existing_nullable=False)
    op.alter_column('accounts', 'name',
               existing_type=sa.VARCHAR(length=256),
               comment=None,
               existing_comment='Human-readable account name',
               existing_nullable=False)
    op.alter_column('accounts', 'external_account_id',
               existing_type=sa.VARCHAR(length=256),
               comment=None,
               existing_comment="Provider's account ID",
               existing_nullable=False)
    op.alter_column('accounts', 'provider_key',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="e.g. 'plaid', 'teller', 'openbb'",
               existing_nullable=False)
    op.create_table_comment(
        'ab_account_mappings',
        'Maps a finance-sync account to an Actual Budget account',
        existing_comment=None,
        schema=None
    )
    op.drop_index(op.f('ix_resolution_audit_log_unresolved_security_id'), table_name='resolution_audit_log')
    op.drop_index(op.f('ix_resolution_audit_log_target_security_id'), table_name='resolution_audit_log')
    op.drop_table('resolution_audit_log')
    op.drop_index(op.f('ix_detected_subscriptions_tenant_id'), table_name='detected_subscriptions')
    op.drop_index(op.f('ix_detected_subscriptions_account_id'), table_name='detected_subscriptions')
    op.drop_table('detected_subscriptions')
    op.drop_index(op.f('ix_unresolved_securities_resolved_security_id'), table_name='unresolved_securities')
    op.drop_index(op.f('ix_unresolved_securities_provider_key'), table_name='unresolved_securities')
    op.drop_table('unresolved_securities')
    op.drop_index(op.f('ix_security_prices_timestamp'), table_name='security_prices')
    op.drop_index(op.f('ix_security_prices_security_id'), table_name='security_prices')
    op.drop_table('security_prices')
    op.drop_index(op.f('ix_enrichment_freshness_security_id'), table_name='enrichment_freshness')
    op.drop_table('enrichment_freshness')
