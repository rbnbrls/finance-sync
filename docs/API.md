# REST API specification

Base URL: `/api/v1`. JSON uses lower camel case externally, RFC 3339 timestamps, decimal values encoded as strings, and ISO currencies. Every collection endpoint supports `limit` (1–500), opaque `cursor`, `from`, `to`, and an `asOf` timestamp where meaningful. Responses include `meta: {asOf, currency, nextCursor, freshness}`.

### Meta envelope — as-of / freshness / coverage

Every aggregate endpoint (`/allocation`, `/cashflow`, `/performance`,
`/subscriptions`, `/net-worth`) declares a `meta` object so clients can
judge how current and how complete the underlying data is:

```json
{
  "meta": {
    "asOf": "2026-08-14T12:00:00Z",
    "freshness": "fresh",
    "coverage": {
      "accounts": 4,
      "holdings": 18,
      "pricedHoldings": 18,
      "staleHoldings": 1,
      "items": 42
    },
    "caveats": ["1 holding(s) have observations older than 24h"]
  }
}
```

| Field | Meaning |
|---|---|
| `meta.asOf` | Timestamp of the underlying data (latest holding/transaction observation); `null` when no data exists. |
| `meta.freshness` | `fresh` (data newer than the 24h horizon), `stale` (older), `unknown` (no data), or `partial` (reserved for mixed per-item freshness). |
| `meta.coverage.accounts` | Accounts included in the computation. |
| `meta.coverage.holdings` | Holdings/positions included (0 for cashflow/subscriptions). |
| `meta.coverage.pricedHoldings` | Holdings with a market value. |
| `meta.coverage.staleHoldings` | Holdings observed more than 24h ago. |
| `meta.coverage.items` | Rows considered (transactions for `/cashflow`, subscriptions for `/subscriptions`). |
| `meta.caveats` | Human-readable data-quality notes (e.g. stale holdings). |

Price cache reads honor a configurable TTL (`PRICE_CACHE_TTL_SECONDS`,
default 24h): cached prices older than the TTL are re-fetched from the
upstream source; when the source is unavailable the cached data is
served with an explicit stale marker (`quote.stale` / `history.stale`
in the enrichment gateway, surfaced as `stale` in price responses).

Authentication is `Authorization: Bearer <JWT>` or `X-API-Key`. Mutations require `Idempotency-Key`; replay returns the original result. Errors use RFC 9457 Problem Details, with a correlation ID.

## Resources

| Method/path | Scope | Contract |
|---|---|---|
| `GET /accounts` | `finance:read` | Accounts, latest balances, institution and connection health. Filters: type, institutionId. |
| `GET /accounts/{id}` | `finance:read` | One account and current snapshot. |
| `GET /transactions` | `finance:read` | Canonical cash transactions. Filters: accountId, provider, status, type, from, to, currency. |
| `GET /holdings` | `investments:read` | Latest (or as-of) holdings; filters accountId, securityId, asOf. |
| `GET /subscriptions` | `subscriptions:read` | List persisted detected subscriptions. Filters: status, confidence. Pagination: limit, offset. |
| `GET /subscriptions/{id}` | `subscriptions:read` | Single detected subscription by ID. |
| `GET /subscriptions/detected` | `subscriptions:read` | Run subscription detection (read-only, ephemeral). Query params: dateFrom, dateTo, minOccurrences. |
| `POST /subscriptions/detect` | `subscriptions:write` | Run integrated subscription detection with request body. Body: { dateFrom?, dateTo?, minOccurrences? }. |
| `POST /subscriptions/analyze` | `subscriptions:write` | Dry-run detection (backward-compatible). Body: { dateFrom?, dateTo?, minOccurrences?, useMerchantClassifier? }. |
| `PATCH /subscriptions/{id}` | `subscriptions:write` | Update subscription status/category/notes. Body: { status?, category?, userNotes? }. |
| `POST /subscriptions/{id}/confirm` | `subscriptions:write` | Confirm a detected subscription as legitimate. Body: { userNotes? }. |
| `POST /subscriptions/{id}/ignore` | `subscriptions:write` | Ignore/dismiss a detected subscription. Body: { reason? }. |
| `DELETE /subscriptions/{id}` | `subscriptions:write` | Permanently remove a subscription record. |
| `GET /portfolio` | `investments:read` | Valuation, cash, gains, positions, freshness. |
| `GET /performance` | `analytics:read` | Time-series and return metrics. Parameters subject, period, from, to. |
| `GET /allocation` | `analytics:read` | Allocation by asset class, sector, country, currency, or security. |
| `GET /net-worth` | `analytics:read` | Cash + investments net-worth series and coverage. |
| `GET /cashflow` | `analytics:read` | Income/expense aggregates and transaction counts. |
| `GET /prices` | `market:read` | Latest/historical prices. Filters: securityId, listingId, interval, from, to. Without a security filter returns the latest price per security. |
| `GET /dividends` | `investments:read` | Dividend-type transactions. Filters: accountId, securityId, from, to. |
| `POST /sync` | `sync:write` | Starts allowed connections; `{providers?, resources?, force?}`. Returns 202 sync-run links. |
| `POST /sync/{provider}` | `sync:write` | Starts one configured provider; provider is registry key, not a URL. |
| `GET /sync-runs/{id}` | `sync:read` | Status, cursors, counts, warnings, error code. |
| `POST /reconciliation` | `reconciliation:write` | Trigger a reconciliation analysis synchronously. Returns the run summary. |
| `GET /reconciliation` | `reconciliation:read` | List reconciliation runs for the tenant. |
| `GET /reconciliation/{id}` | `reconciliation:read` | Get a reconciliation run with its findings. Findings referencing accounts outside the principal's visibility scope are hidden. |
| `GET /household/members` | `household:read` | List household members and their roles. Visible to every member. |
| `PATCH /household/members/{id}/role` | `household:write` | Change a member's role (`admin` / `user`). Admin only. |
| `DELETE /household/members/{id}` | `household:write` | Remove a member from the household. Admin only. |
| `POST /household/invitations` | `household:write` | Create a single-use, expiring invitation. Admin only. |
| `GET /household/invitations` | `household:read` | List pending invitations. Admin only. |
| `POST /household/invitations/accept` | public (token) | Accept an invitation with `{token}`; signs the new member in. |
| `POST /household/invitations/{id}/revoke` | `household:write` | Revoke a pending invitation. Admin only. |
| `GET /household/audit-log` | `household:read` | Household audit trail (invitations, role changes, visibility changes, cleanup decisions) — no financial payloads. Admin only. |
| `GET /accounts/{id}/share-preview` | `finance:read` | Impact preview of sharing an account: transactions, holdings, balance snapshots and current balance that enter/leave the household view. |
| `PATCH /accounts/{id}/visibility` | `finance:write` | Set `private` or `household` visibility. Owner only; reports `export_cleanup_required` + `export_artifacts` when unsharing an account with prior exports. |
| `POST /accounts/{id}/claim` | `finance:write` | Claim a system-owned (unowned) account. Admin only. |
| `GET /accounts/{id}/export-artifacts` | `finance:read` | Describe previously exported data for an account (files, mappings, deliveries) — drives the unshare cleanup flow. Owner only. |
| `POST /accounts/{id}/export-quarantine` | `finance:write` | Non-destructively move the account's export CSVs aside (mapping/delivery kept). Owner only. |
| `POST /accounts/{id}/export-cleanup` | `finance:write` | Permanently delete export files + mapping + delivery, only with `{confirm: true}`. Owner only. |
| `GET /health` | public/internal | Liveness/readiness/dependency checks; redact details publicly. |
| `GET /metrics` | internal | Prometheus exposition, network-restricted. |

## AI resources

AI routes require `ai:read`, accept `currency` and `asOf`, and intentionally return bounded, source-cited summaries rather than raw paginated ledgers.

| Path | Response focus |
|---|---|
| `GET /ai/context` | Data coverage, accounts/portfolios, total values, freshness and caveats. |
| `GET /ai/networth` | Current and trailing series, component deltas, valuation coverage. |
| `GET /ai/portfolio` | Holdings, allocation, gains, concentration and stale prices. |
| `GET /ai/monthly-summary` | Income, expenses, cash-flow, notable changes for requested month. |
| `GET /ai/dividends` | Paid/expected dividend summary and recent events. |
| `GET /ai/subscriptions` | Recurring-payment candidates with confidence and evidence transaction IDs. |

Example response shape:

```json
{"data":{"asOf":"2026-07-20T10:00:00Z","currency":"EUR","netWorth":"125000.00","coverage":{"accounts":4,"pricedHoldings":18,"staleHoldings":1},"caveats":["One US listing price is 18 minutes old"]},"meta":{"correlationId":"...","freshness":"partial"}}
```

Version only breaking changes in `/api/v2`; add optional fields and endpoints without a major version. Publish OpenAPI at `/openapi.json`, Swagger at `/docs`, and a generated client only after API contract tests are established.

## Household & account sharing

Every financial account belongs to exactly one household (tenant) and has an
owner plus an explicit visibility policy. The model is **private by default**:
accounts migrated from before household sharing stay system-owned and are only
visible to tenant admins until claimed.

### Visibility policy

A principal may read an account when **any** of the following holds:

| Principal | May read |
|---|---|
| Owner of the account | Always (incl. `private` accounts) |
| Any household member | `household`-visible (shared) accounts |
| Tenant admin | `household` accounts, own accounts, and system-owned (unclaimed) accounts |
| API key / machine principal | `household` accounts and system-owned accounts — never a user's `private` accounts |

`PATCH /accounts/{id}/visibility` accepts `private` (only the owner sees the
account) or `household` (every household member sees it). Only the owner may
change visibility; admins can claim system-owned accounts so every account
eventually has an owner.

### Side-channel guarantees

The same policy is enforced — with automated multi-user integration tests —
across every surface derived from accounts, so a member can never infer another
member's private data through a totals column, a filter, an export, a webhook
event, an MCP tool, or an error message:

- **Read APIs & aggregates** — accounts, transactions, holdings, balances, net
  worth, cashflow, dividends, portfolio, allocation, performance all scope to
  the principal's visible accounts (SQL subquery, applied inside the query).
- **Subscriptions** — `GET /subscriptions`, `GET /subscriptions/{id}`,
  `PATCH/POST/DELETE` row operations, and detection/analysis runs only see
  subscriptions detected from visible accounts. Detection never reads
  transactions of another member's private accounts.
- **Tax lots** — list and summary are scoped; explicit `account_id` filters for
  invisible accounts return nothing.
- **Reconciliation** — run detail hides findings that reference accounts
  outside the principal's scope (404-equivalent).
- **Derived rows** — scheduled payments and card transactions are scoped like
  transactions.
- **AI summaries** — the prompt context only contains the principal's visible
  accounts; private names, balances and transactions never reach the model.
- **Exports** — the Wealthfolio/Actual Budget exporters only export
  `household`-visible accounts; revoking a share halts export on the next run
  (defense-in-depth filter in the push path).
- **Webhooks** — events for private accounts are suppressed for non-owners in
  a household; aggregate events are never suppressed.
- **MCP** — `get_subscriptions` (and all read tools) apply the same scope.

### Revocation & export cleanup

Unsharing an account that was previously exported triggers a cleanup flow —
nothing is deleted silently. `PATCH /accounts/{id}/visibility` to `private`
returns `export_cleanup_required` plus `export_artifacts`; the owner then either
quarantines (non-destructive CSV move, mapping kept) or permanently deletes with
an explicit `confirm: true`. Every decision is written to the household audit
log with sanitised payloads (no financial data).

### Roles & RBAC

| Action | Admin | User |
|---|---|---|
| View members list | ✓ | ✓ |
| Invite / revoke invitations | ✓ | — |
| Accept an invitation | ✓ (via token) | ✓ (via token) |
| Change roles / remove members | ✓ | — |
| View audit log | ✓ | — |
| Share / unshare own accounts | ✓ (own accounts only) | ✓ (own accounts only) |
| Claim unowned accounts | ✓ | — |
| Export cleanup (quarantine/delete) | ✓ (own accounts only) | ✓ (own accounts only) |

## CLI: Reconciliation

Two CLI commands are available for ad-hoc reconciliation runs.

### `reconcile` — Full analysis

```
python -m finance_sync reconcile [OPTIONS]
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--tenant-id` | (all tenants) | Reconcile a single tenant instead of all tenants. |
| `--account-ids` | (all accounts) | Comma-separated list of account IDs to analyze. |
| `--provider-keys` | (all providers) | Comma-separated provider/connector keys to compare (e.g. `bunq,trading212`). |
| `--date-from` | (90 days ago) | Explicit start date in ISO-8601 format (e.g. `2026-01-01` or `2026-01-01T00:00:00Z`). Overrides `--days-back`. |
| `--date-to` | (now) | Explicit end date in ISO-8601 format. Overrides `--days-back`. |
| `--days-back` | 90 | Look-back window for the analysis (ignored when `--date-from`/`--date-to` are set). |
| `--threshold-hours` | 48 | Max hour gap for duplicate candidates. |

Exit codes:

- **0** — Success, no discrepancies found.
- **1** — Success, discrepancies detected.
- **2** — Error (settings, DB, unexpected exception).

### `compare` — Connector comparison

```
python -m finance_sync compare <connector_a> <connector_b> [OPTIONS]
```

Arguments:

| Argument | Description |
|----------|-------------|
| `connector_a` | First connector/provider key (e.g. `bunq`). |
| `connector_b` | Second connector/provider key (e.g. `trading212`). Must differ from `connector_a`. |

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--tenant-id` | (first tenant) | Tenant ID to reconcile. |
| `--date-from` | (90 days ago) | Explicit start date in ISO-8601 format. |
| `--date-to` | (now) | Explicit end date in ISO-8601 format. |
| `--threshold-hours` | 48 | Max hour gap for duplicate candidates. |

Exit codes match the `reconcile` command.

Both commands create the same `ReconciliationService` used by the API endpoint,
so findings are stored in the database and visible through API queries.

## CLI: Actual Budget exporter

The Actual Budget exporter is triggered from the CLI (the only supported
trigger). The `finance-sync` console script (installed via pip) is equivalent
to `python -m finance_sync`.

### `actual-budget export` — Full export cycle

```
finance-sync actual-budget export [OPTIONS]
```

Runs a full export cycle against the configured Actual Budget server:
resolves or creates AB accounts, imports pending transactions via the
reconcile (dedup-aware) flow, and writes a CSV summary for manual import.

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--output-dir` | `/tmp/finance_sync_ab_exports` | Directory for the CSV summary file. |
| `--account-ids` | (all active) | Comma-separated list of account IDs to export. |
| `--days-back` | 90 | Days of transaction history to export. |
| `--max-transactions` | (unlimited) | Hard limit on transactions to export per run. |

Configuration (env vars or settings):

| Variable | Description |
|----------|-------------|
| `EXPORTER_ACTUAL_BUDGET_ENABLED` | Master switch for the Actual Budget exporter (default `true`). When `false`, the CLI commands below exit with code 2 and the exporter is omitted from `GET /exporters/types`. |
| `ACTUAL_BUDGET_SERVER_URL` | Actual Budget server URL (e.g. `http://localhost:5006`). |
| `ACTUAL_BUDGET_PASSWORD` | Server password (Settings → Show advanced). |
| `ACTUAL_BUDGET_BUDGET_NAME` | Budget file display name. |
| `ACTUAL_BUDGET_SYNC_ID` | Budget sync ID (UUID); takes precedence over the name. |
| `ACTUAL_BUDGET_ENCRYPTION_PASSWORD` | E2E encryption password, if the budget is encrypted. |

Exit codes: **0** — completed, **2** — error (connection, config, DB).

### `actual-budget push` — Push to a running server

```
finance-sync actual-budget push [OPTIONS]
```

Pushes pending transactions to a running Actual Budget instance. Requires a
server URL and password (env vars or `--server-url`/`--password`).

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--server-url` | `ACTUAL_BUDGET_SERVER_URL` | Actual Budget server URL. |
| `--password` | `ACTUAL_BUDGET_PASSWORD` | Actual Budget server password. |
| `--account-ids` | (all active) | Comma-separated list of account IDs to push. |
| `--days-back` | 90 | Days of transaction history to push. |
| `--max-transactions` | (unlimited) | Hard limit on transactions to push per run. |
| `--dry-run` | off | Count pending transactions without pushing. |

Exit codes: **0** — completed (or dry-run finished), **2** — error.

`finance-sync wealthfolio smoke --account-ids <id>` voert twee account-scoped
pushes uit en controleert account-, activity- en holdingszichtbaarheid plus
idempotentie. De uitvoer bevat geen financiële waarden of credentials. Zie
`docs/degiro-wealthfolio.md` voor de activity-first holdingsstrategie en
reconciliatietoleranties.

## Exporters: feature flags

Both exporters are behind per-exporter kill switches (roadmap dr.3 / gap
G-13), so unfinished exporter work never ships without an operator
override.

| Variable | Default | Effect when `false` |
|----------|---------|---------------------|
| `EXPORTER_WEALTHFOLIO_ENABLED` | `true` | `GET /exporters/config` and `POST /exporters/export` return **404** ("Wealthfolio exporter is disabled"); `GET /exporters/types` omits `wealthfolio`; `finance-sync wealthfolio export/push` exits with code 2. |
| `EXPORTER_ACTUAL_BUDGET_ENABLED` | `true` | `GET /exporters/types` omits `actual-budget`; `finance-sync actual-budget export/push` exits with code 2. |

Defaults are `true` for both: the exporters ship enabled, matching the
historical behaviour — the Actual Budget R1 CLI triggers landed in PR #201,
so there is no unfinished exporter surface to protect by default. Toggling
a flag enables/disables the exporter's API and CLI surface without a code
change. `GET /exporters/runs` history is not gated — it remains readable as
audit data regardless of the flags.

## Exporters: run history, DLQ visibility and retry

Export runs (both exporters) are recorded in `export_runs` and exposed for
audit and dead-letter handling:

| Method/path | Description |
|---|---|
| `GET /exporters/runs` | List export runs (newest first). Filters: `status` (`running`, `completed`, `failed`, `cancelled`; `error` is an alias for `failed`). Every run carries `exporter_type` and `error_message` (populated when the run failed). Pagination: `limit`, `offset`. |
| `GET /exporters/runs/{id}` | One export run with its error detail. |
| `POST /exporters/{type}/runs/{id}/retry` | Re-run a **failed** run for `type` ∈ `wealthfolio`, `actual-budget` using that exporter's export cycle (the same cycle `POST /exporters/export` triggers: CSV generation for Wealthfolio, cursor-based export for Actual Budget). Creates a fresh `ExportRun` (returned as `run_id`); the original failed run is kept for audit. Delivery itself is idempotent: the Actual Budget cycle resumes from its per-account `export_deliveries` cursor, and the Wealthfolio push path (`finance-sync wealthfolio push` / the worker sweep) resumes from its per-account `wealthfolio_deliveries` cursor, so already-delivered transactions are never re-pushed or duplicated. Returns 404 for unknown run/type or disabled exporter, 409 when the run is not failed or belongs to a different exporter type. |

The Wealthfolio push path (`finance-sync wealthfolio push` / the worker
sweep) tracks each push as an `ExportRun` and maintains a per-account
`wealthfolio_deliveries` cursor: after a partial failure the run is marked
`failed` with per-account error detail, and the next push (or retry) only
re-processes the accounts whose cursor did not advance. Activities carry the
stable remote `accountId`; account mappings are persisted in
`wealthfolio_account_mappings`.
