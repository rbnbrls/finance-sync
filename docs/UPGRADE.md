# Upgrade Notes (UPGRADE.md)

Operator-facing notes for every schema migration and breaking change since
the initial schema (revision `0001`). Read this before upgrading a running
deployment, and follow the **Operator action** for each step you cross.

- **How upgrades run:** migrations are applied with Alembic **before** the
  new application version starts (the release pipeline runs a dedicated
  migration job; see `docs/MIGRATIONS.md`). The application itself never
  creates or alters schema at runtime.
- **Current head:** `0024`
- **Chain:** `0001 → 0002 → … → 0024` (single linear chain, verified by CI)

| Quick reference | |
|---|---|
| Apply pending migrations | `ASYNC_DB_URL=<url> alembic upgrade head` |
| Inspect the chain | `ASYNC_DB_URL=<url> alembic history` |
| Verify schema parity with ORM | `ASYNC_DB_URL=<url> alembic check` |
| Full rollback (dev only) | `ASYNC_DB_URL=<url> alembic downgrade base` |

> **Rollback policy (production):** production rollback is **image
> rollback + backward-compatible migrations**, never a blind schema
> downgrade. Every migration below has a working `downgrade()`, but
> downgrading in production drops data and can strand the previous image.
> Use `alembic downgrade` only in development / scratch environments.
> See [Rollback notes](#rollback-notes) at the bottom.

The release smoke gate runs on staging with synthetic financial fixtures only.
It records health, readiness, sync/outbox and exporter evidence together with
the commit and immutable image tag. This evidence is an operational check; it
never contains credentials or financial payloads.

Release 13 closeout evidence includes the immutable image tag, migration
artifact link and staging smoke artifact link. Rollback remains an image
rollback against backward-compatible migrations, never an automatic
production downgrade.

---

## Milestone 1 — Foundation (revisions 0001–0003)

Ships the deployable skeleton: core financial schema, authentication, and
webhook delivery. Fresh installs land here first.

### 0001 — Initial canonical schema

- **Schema changes:** creates the core tables: `tenants`, `users`,
  `accounts`, `securities`, `security_listings`, `transactions`,
  `holdings`, `balances`, `outbox_messages`, `sync_runs`.
- **Config changes:** none.
- **Operator action:** nothing beyond a working `DATABASE_URL` /
  `POSTGRES_*` connection; the app seeds a default tenant + admin user on
  first boot (lifespan seeding, unchanged by any later release).
- **Rollback:** `downgrade` drops all ten core tables — **total data
  loss**. Never downgrade past this point on a real database.

### 0002 — Auth tables

- **Schema changes:** adds `api_keys` and `credentials` (envelope-encrypted
  provider credentials: `encrypted_payload` + `nonce`).
- **Config changes:** storing provider credentials now requires
  `MASTER_ENCRYPTION_KEY` (hex-encoded 32-byte AES-256-GCM key, see
  `.env.example`). Without it, credential creation fails.
- **Operator action:** set `MASTER_ENCRYPTION_KEY` and keep it stable —
  credentials are encrypted with it and **cannot be decrypted if the key
  changes or is lost** (export/rotate deliberately unsupported).
- **Rollback:** drops `credentials` and `api_keys`; provider sync will fail
  until credentials are re-entered through the API.

### 0003 — Webhook tables

- **Schema changes:** adds `webhooks` (event subscriptions, JSONB `events`
  GIN index) and `webhook_delivery_logs` (delivery attempts, retry index).
- **Config changes:** none.
- **Operator action:** none (webhooks are tenant-managed via the API).
- **Rollback:** drops both tables; webhook subscriptions and delivery
  history are lost.

---

## Milestone 2 — Ingestion (revision 0006, plus 0001)

Reliable provider-neutral facts: canonical accounts/transactions from 0001,
plus reconciliation and payments.

### 0006 — Phase 3 reconciliation / payments

- **Schema changes:** adds `reconciliation_runs` + `reconciliation_results`
  (cross-connector gap detection) and `scheduled_payments` +
  `card_transactions` (bunq scheduled payments / card payments).
- **Config changes:** none.
- **Operator action:** none — reconciliation is flag-gated
  (`worker_job_reconciliation_enabled`, default on); scheduled
  payments/cards ingestion lands behind a feature flag when wired into the
  sync pipeline (roadmap G-04).
- **Rollback:** drops the four tables; stored reconciliation findings and
  payment rows are lost (safe to re-derive from a re-sync).

> **Note on `card_transactions.account_id`:** originally `NOT NULL` here,
> relaxed to nullable in revision 0010 — see that note before ingesting
> card payments.

---

## Milestone 3 — Enrichment (revisions 0004–0005)

Market-data-backed security projections.

### 0004 — Fundamentals & ETF metadata

- **Schema changes:** adds `fundamental_observations` and
  `security_metadata_observations` (per-security enrichment snapshots with
  `source` provenance, FK to `securities`).
- **Config changes:** optional OpenBB gateway settings (base URL, API
  version, `openbb_rate_limit_rps`); the gateway degrades gracefully
  without a key.
- **Operator action:** none (additive tables, populated by enrichment).
- **Rollback:** drops both tables; fundamental/metadata history is lost but
  re-fetchable from OpenBB.

### 0005 — FX rates

- **Schema changes:** adds `fx_rates` (unique
  `(base_currency, quote_currency, timestamp, source)`, composite index).
- **Config changes:** none.
- **Operator action:** none (additive; populated by the FX service with
  graceful degradation when the provider is down).
- **Rollback:** drops `fx_rates`; currency conversion falls back to a
  degraded path until rates are re-fetched.

---

## Milestone 4 — Consumer API (revision 0008)

Stable downstream contracts (Actual Budget / Wealthfolio exporters).

### 0008 — Exporter tables

- **Schema changes:** adds `export_runs` (run tracking), `ab_account_mappings`
  (Actual Budget account-id mapping, unique per account), and
  `export_deliveries` (idempotent per-account delivery cursors for Actual
  Budget). These tables previously existed **only via
  `Base.metadata.create_all`** at app startup; they are now
  Alembic-managed.
- **Config changes:** per-exporter feature flags
  `EXPORTER_ACTUAL_BUDGET_ENABLED` / `EXPORTER_WEALTHFOLIO_ENABLED`
  (default `true`; see `.env.example`). Toggling a flag off hides that
  exporter's API surface and CLI commands.
- **Operator action:** **none required for this migration itself**, but this
  revision is the first one that removes the `create_all` schema fallback
  (see the G-01 reconciliation section below) — from here on you **must**
  run `alembic upgrade head` before starting the new app image.
- **Rollback:** drops the three tables; export history, account mappings and
  delivery cursors are lost. Re-running an export after rollback restarts
  from scratch (a full re-export may re-create consumer-side duplicates).

---

## Milestone 5 — Automation / insights (revision 0009, partial)

AI summaries, Home Assistant integration, Grafana dashboards, performance
analytics, subscription detection. No dedicated schema migrations were
shipped with the original M5 features; the tables M5 needs
(`detected_subscriptions`, plus enrichment/price/audit tables) arrived with
the schema-to-ORM sync revision **0009** (see below).

### 0009 — Sync schema to ORM (drift closure)

Closes the pre-existing drift between the ORM models and the migrated
schema (found by `alembic check` once the `create_all` fallback was gone):

- **Schema changes:**
  - **Creates** `enrichment_freshness`, `security_prices`,
    `unresolved_securities`, `detected_subscriptions` and
    `resolution_audit_log` — all previously created only via
    `create_all`, never migrated.
  - **Adds** `outbox_messages.idempotency_key` (unique) so exactly-once
    outbox delivery is enforced at the schema level.
  - **Aligns** column types, comments, indexes and tenant FKs with the ORM
    (`webhook_delivery_logs.webhook_id` and
    `reconciliation_results.transaction_id_a/b` switch from UUID to the
    model-declared string type; phase-3 tables lose their `ON DELETE
    CASCADE` tenant FKs).
- **Config changes:** none.
- **Operator action:** for databases created via `create_all` this is the
  migration that finally matches the schema to the models — see the
  reconciliation section below. If any existing outbox rows share a
  non-null `idempotency_key`, the unique constraint creation fails; dedupe
  or null out the offending keys first (in practice keys are new, so this
  is unlikely).
- **Rollback:** drops the five created tables, removes
  `outbox_messages.idempotency_key`, and reverts the type/FK/comment
  alignment. **Destructive** to enrichment freshness, price history,
  unresolved-securities queue, subscription detections and resolution
  audit logs — re-derivable but expensive. Avoid in production.

---

## Milestone 6 — Ecosystem (revision 0007)

Plugin SDK, MCP server, additional connectors, tax lots.

### 0007 — Tax lots

- **Schema changes:** adds `tax_lots` (FIFO lot accounting: purchase/sale
  transaction FKs, realized P/L, wash-sale flag, cost-basis method) and
  **adds `transactions.quantity`** to the existing `transactions` table.
- **Config changes:** none.
- **Operator action:** none — `transactions.quantity` is additive and
  nullable; tax-lot computation backfills from holdings.
- **Rollback:** drops `tax_lots` and removes `transactions.quantity`; tax
  calculations fail until re-upgraded. `downgrade` drops the column even if
  it holds data — verify before running.

---

## Cross-cutting changes (not tied to a single milestone)

### G-01: `create_all` removed — Alembic is now the only schema owner

PR #196 (fix of the migration chain) removed the
`Base.metadata.create_all` fallback and the `ALEMBIC_HEAD="0003"` stamp from
`src/finance_sync/lifespan.py`. The application **no longer creates tables
at startup**.

- **Before:** a fresh deployment self-created the full schema at boot.
- **After:** `alembic upgrade head` must run before the app starts (the
  release pipeline does this; CI enforces it on an empty PostgreSQL).
- **Failure mode:** starting the new image without running migrations
  leaves a half-created or missing schema — the app will error on first
  query instead of auto-healing.

### Re-numbering of the 0004-family revisions (G-01)

The four Phase-3 revisions all originally declared `revision="0004"` with
`down_revision="0003"` (duplicate IDs — `alembic upgrade head` was
unresolvable). They were renumbered into the linear chain:

| Old (duplicate) file | New revision |
|---|---|
| `0004_add_fundamentals_metadata_tables` | `0004` |
| `0004_add_fx_rates` | `0005` |
| `0004_add_phase3_tables` | `0006` |
| `0004_add_tax_lots` | `0007` |

No tables were dropped or recreated by the renumbering — it is purely a
revision-ID change. Newly created databases (`alembic upgrade head` from
empty) are unaffected.

### Reconciling a database created via the old `create_all` path

If your database was created by the old app startup (schema built by
`Base.metadata.create_all`, `alembic_version` stamped `0003` or absent),
the schema on disk already contains **all** ORM tables — including the
export tables, `enrichment_freshness`, `security_prices`,
`unresolved_securities`, `detected_subscriptions`, `resolution_audit_log`
and `tax_lots` — because `create_all` created every model table. The
migration chain has nothing to create for them; it only needs to record
that they exist and then apply the small deltas (outbox idempotency key,
type/FK alignment, nullable `account_id`).

Recommended reconciliation (keeps data):

1. **Back up first** — `pg_dump` the database.
2. **Stamp the chain as applied** (do **not** run `upgrade` yet — it would
   fail on `table already exists`):
   ```bash
   ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
       alembic stamp head
   ```
3. **Verify parity** with the ORM:
   ```bash
   ASYNC_DB_URL=postgresql+asyncpg://user:pass@host:5432/finance_sync \
       alembic check
   ```
   - **Clean** → done; the schema is at head. `upgrade head` becomes a
     no-op from now on.
   - **Reports drift** (e.g. a column added to a model after your database
     was last booted, such as `outbox_messages.idempotency_key`) →
     apply the missing objects by hand following the migration content, or
     use the alternative path below.
4. Optionally verify with the integration suite's migration test
   (`make test-integration`).

Alternative path (clean, loses app data — use only for throwaway/dev
databases): dump the old DB, create a fresh database, `alembic upgrade
head`, then restore only the data you need.

### Deploying the new app image

Upgrade order for a running deployment:

1. Run migrations against the production database
   (`alembic upgrade head` — the release pipeline does this in a
   dedicated migration job).
2. Roll out the new application image(s).
3. Verify `/health/ready` (checks DB reachability) and a sync run.

### 0021–0024 — Destination wizard and target-scoped delivery

- **Schema changes:** `0021` adds `export_targets`; `0022` scopes Actual
  Budget and Wealthfolio mappings/cursors by destination, with current rows
  backfilled as `legacy`; `0023` adds the destination version field; `0024`
  adds an optional account allowlist to API keys for Jupyter consumers.
- **Operator action:** run `alembic upgrade head` before deploying. Existing
  `WEALTHFOLIO_*` and `ACTUAL_BUDGET_*` settings are read once at startup to
  bootstrap an equivalent legacy destination when no destination of that type
  exists. Afterwards manage new connections in **Bestemmingen**; do not set a
  second global schedule, as each active destination owns its own schedule.
- **Secrets:** retain `MASTER_ENCRYPTION_KEY`. It decrypts the migrated
  destination credential; rotating or losing it makes the connection
  unrecoverable until the credential is re-entered.
- **Rollback:** application-image rollback is safe because the migrations are
  additive. Do not run a production downgrade: it drops destination records
  and removes per-destination delivery scope.

### Config additions since `0001` (breaking only where noted)

| Variable | Since | Notes |
|---|---|---|
| `MASTER_ENCRYPTION_KEY` | 0002 | **Required** for credential storage; changing it orphans stored credentials |
| `ASYNC_DB_URL` | G-01 | Alembic connection string; DDL-only migration user recommended (`scripts/setup-migration-user.sql`) |
| `EXPORTER_ACTUAL_BUDGET_ENABLED` / `EXPORTER_WEALTHFOLIO_ENABLED` | 0008 / PR #202 | Default `true`; set `false` to disable an exporter's API + CLI surface |
| `GRAFANA_ALERT_WEBHOOK_URL` / `GRAFANA_ALERT_EMAILS` | PR #207 | Grafana alerting channels; default webhook is a no-op placeholder |
| `COOLIFY_API_TOKEN`, `GITHUB_TOKEN`, `STATE_FILE` | PR #206 | For the in-repo `finance-sync-monitor` (env-only; scheduled via `deploy/systemd/`) |

---

## Rollback notes

- **Per-revision:** every revision `0001`–`0010` has a symmetric
  `downgrade()`. Table above lists what each downgrade destroys. In
  general: `0001`, `0002`, `0008`, `0009` downgrades are destructive;
  `0004`, `0005`, `0006`, `0007` lose re-derivable data; `0003`, `0010`
  are the least risky.
- **0010 downgrade specifically:** restores `NOT NULL` on
  `card_transactions.account_id` — **only safe if no rows carry a NULL
  account id**. Check first:
  ```sql
  SELECT count(*) FROM card_transactions WHERE account_id IS NULL;
  ```
- **Production:** roll back by redeploying the previous image (migrations
  are backward-compatible / expand-contract — old code reads the new
  schema fine). Only downgrade the schema if the previous image cannot
  start against it, and only after a backup.
