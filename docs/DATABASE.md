# PostgreSQL data model

All tables use UUID primary keys, `tenant_id`, `created_at`, and `updated_at` unless immutable. Monetary values use `numeric(24,8)` plus ISO-4217 `currency_code`; timestamps are `timestamptz` in UTC. Provider IDs are strings, never integers. No floats are used for money, quantities, or prices.

## Core tables

| Table | Purpose and key constraints |
|---|---|
| `tenant` | Logical ownership boundary; unique slug. |
| `user`, `role`, `user_role`, `api_key`, `refresh_token` | Identity and access. API-key/token values are hashes; keys unique by public prefix. |
| `institution` | Provider/bank/broker metadata; unique `(provider_key, external_institution_id)` where available. |
| `connection` | Encrypted credential envelope, config, health state. Unique `(tenant_id, provider_key, logical_name)`. |
| `account` | Canonical cash/bank/broker account. Unique `(connection_id, external_id)`; supports soft deletion. |
| `account_balance_snapshot` | Point-in-time available/booked balances; unique `(account_id, observed_at, balance_kind)`. |
| `transaction` | Canonical cash movement. Unique `(connection_id, external_id)` and `provider_fingerprint`; immutable financial fields after booking, with revision metadata. |
| `transaction_leg` | Optional double-entry/categorization legs; check non-zero amount. |
| `scheduled_payment`, `payment_request`, `card` | Provider-neutral planned/auxiliary banking objects with external identity uniqueness. |
| `portfolio` | Investment grouping; unique `(connection_id, external_id)`. |
| `cash_position_snapshot` | Portfolio cash by currency/time; unique `(portfolio_id, currency_code, observed_at)`. |
| `security` | Instrument identity: ISIN/FIGI/CUSIP where known; unique non-null identifiers, plus canonical name/type. |
| `security_listing` | Tradable symbol + MIC/exchange + currency; unique `(symbol, mic, currency_code)`. |
| `holding_snapshot` | Quantity/cost/value at observation time; unique `(portfolio_id, security_id, observed_at)`. |
| `investment_trade` | Buy/sell/fee corporate-action-derived execution; unique `(connection_id, external_id)`. |
| `dividend` | Entitlement/payment facts; unique `(connection_id, external_id)` or provider fingerprint. |
| `price_observation`, `fx_rate` | Immutable market observations, unique by instrument/pair, source, timestamp, and granularity. |
| `security_metadata_observation`, `fundamental_observation` | Versioned OpenBB-derived enrichment and source/freshness metadata. |
| `performance_snapshot` | Computed portfolio/account/tenant return metrics; unique `(subject_type, subject_id, period, as_of)`. |
| `sync_run`, `sync_cursor`, `raw_payload` | Sync audit, incremental cursor, encrypted source data; cursors unique by `(connection_id, resource)`. |
| `outbox_event`, `event_delivery` | Durable event stream and consumer delivery idempotency. Event ID unique; delivery unique `(event_id, consumer_name)`. |
| `export_target`, `export_delivery` | Exporter configuration and replay-safe downstream delivery; unique `(target_id, source_event_id, payload_version)`. |
| `audit_log` | Security-sensitive mutation history; append-only. |

## Indexes, integrity, and lifecycle

- Index every tenant-filtered access path: e.g. `(tenant_id, occurred_at DESC)` on transactions; `(tenant_id, as_of DESC)` on summaries; `(portfolio_id, observed_at DESC)` on holdings.
- Partial indexes for current records: `WHERE deleted_at IS NULL`, pending outbox events, and active connections.
- Use `CHECK` constraints for ISO code length, non-negative quantities where applicable, valid enum status, `valid_to >= valid_from`, and non-zero transaction amount. Use foreign keys with restrictive deletes for financial records; soft-delete user-facing configuration.
- Partition high-volume immutable observations (`transaction`, `price_observation`, `raw_payload`, `audit_log`, `outbox_event`) monthly after operational volume justifies it. Start unpartitioned to reduce migration complexity.
- Alembic owns all schema changes. Each revision is backward-compatible first (expand), app code migrates/read-dual, then a later release contracts. Production migration runs once as a release job, with backup and tested rollback plan.

## Accounting semantics

Transactions have `status` (`pending`, `booked`, `reversed`, `cancelled`), `occurred_at`, `booked_at`, signed amount, counterparty and provider type. A provider may change pending transactions; booked revisions preserve source revision/fingerprint. Holdings and balances are snapshots rather than overwritten current values; read models select latest valid snapshot. This preserves historical net-worth and reconciliation capability.
