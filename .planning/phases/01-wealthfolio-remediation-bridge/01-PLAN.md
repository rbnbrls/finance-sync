# Phase 1 Plan: Wealthfolio remediation bridge

## Objective

Implement a pull-based integration that converts Wealthfolio Data Health
issues into finance-sync remediation items and safely repairs the subset that
can be fixed from canonical finance-sync data.

## Design decisions

1. Use polling from finance-sync. Wealthfolio does not currently provide a
   suitable webhook for its health endpoint.
2. Reuse the encrypted Wealthfolio password already stored in `ExportTarget`;
   never add a second plaintext credential store.
3. Treat finance-sync as the canonical source. The bridge must not invent
   purchase prices, transactions, transfers, or valuations.
4. Use one backlog item per target, issue category, and affected remote
   entity. Store only bounded, sanitized context.
5. Reuse the existing enrichment and Wealthfolio quote projection paths where
   possible. Extract shared logic instead of implementing a second quote
   writer.
6. Unknown Wealthfolio issue categories fail closed into `manual_review`.

## Work breakdown

### 1. Add Wealthfolio health client contract

Relevant files:

- `src/finance_sync/exporter/wealthfolio/client.py`
- `src/finance_sync/exporter/wealthfolio/`

Tasks:

- Add `get_health_status()` using the existing authenticated client.
- Add a targeted market-data sync method if required by the installed
  Wealthfolio API.
- Normalize HTTP 401/403, 408, 429, 5xx, timeout, and malformed JSON errors.
- Add bounded retries only for transient failures.
- Ensure logs never include passwords, API keys, or full response payloads.

Verification:

- Mocked client tests cover success, auth failure, timeout, rate limit,
  malformed response, and unknown fields.

### 2. Add bridge state and migration

Relevant files:

- `src/finance_sync/models/`
- `migrations/`
- `src/finance_sync/models/__init__.py`

Tasks:

- Add a tenant-scoped `wealthfolio_health_cursors` table keyed by target.
- Store last successful poll, payload hash, issue count, and bounded error
  state.
- Add indexes and a migration.
- Do not store raw health payloads or destination secrets.

Verification:

- Migration applies cleanly to an empty and existing database.
- Cursor rows are isolated by tenant and target.

### 3. Normalize health issues and enqueue remediation

Relevant files:

- New `src/finance_sync/services/wealthfolio_health_bridge.py`
- `src/finance_sync/services/remediation.py`
- `src/finance_sync/reconciliation/remediation/backlog.py`
- `src/finance_sync/models/remediation.py`

Tasks:

- Parse `issues`, `fixAction`, `affectedItems`, severity, code, and bounded
  details from Wealthfolio.
- Resolve Wealthfolio asset IDs to canonical securities by ISIN, ticker, and
  configured provider symbol.
- Create stable `DetectedIssue` records with target ID and remote entity in
  context.
- Add issue mappings:
  - quote sync failure → `wealthfolio_quote_sync_failure`;
  - missing historical prices → `wealthfolio_historical_price_gap`;
  - missing purchase price → `wealthfolio_missing_purchase_price`;
  - negative/incomplete valuation → manual-review issue;
  - unknown category → `wealthfolio_unsupported_issue`.
- Reconcile active bridge items that are no longer present in a subsequent
  successful health response, subject to verification rules.

Verification:

- Same health payload twice produces no duplicate items.
- Two affected assets produce two independently traceable items.
- A missing issue is not resolved when the latest poll failed.
- Unknown issue types go to `manual_review`.

### 4. Implement safe remediation strategies

Relevant files:

- `src/finance_sync/reconciliation/remediation/quote.py`
- `src/finance_sync/reconciliation/remediation/price_history.py`
- `src/finance_sync/reconciliation/remediation/executor.py`
- `src/finance_sync/worker/jobs.py`
- `src/finance_sync/exporter/wealthfolio/exporter.py`

Tasks:

- Add a Wealthfolio-aware quote repair strategy or a reusable service layer
  around the existing quote enrichment flow.
- For quote issues, ensure a recent canonical quote exists, then project it to
  Wealthfolio using the existing `upsert_quote` contract.
- For historical gaps, enrich the requested bounded date range and project
  the resulting quote history.
- Refactor `_sync_quote_history` so scheduled export and remediation share the
  same quote payload and identity matching logic.
- Add remote verification through `/health/status` before transitioning an
  item to `resolved`.
- Route missing purchase prices, negative valuations, and unknown issues to
  `manual_review`; never synthesize financial facts.
- Ensure strategy quota accounting and retry behavior work without treating a
  Wealthfolio target as a connector credential.

Verification:

- Successful quote repair updates the canonical price store and Wealthfolio.
- A provider 404 becomes retryable or manual review according to its class.
- An unsafe transaction issue never mutates canonical financial data.
- Verification failure produces `retry_wait`, not `resolved`.

### 5. Integrate with scheduler and configuration

Relevant files:

- `src/finance_sync/worker/scheduler.py`
- `src/finance_sync/worker/jobs.py`
- `src/finance_sync/config/settings.py`
- `.env.example`

Tasks:

- Add configuration:
  - `WEALTHFOLIO_HEALTH_BRIDGE_ENABLED=false`;
  - `WEALTHFOLIO_HEALTH_BRIDGE_INTERVAL_MINUTES=15`;
  - bounded target and issue limits.
- Add a scheduled bridge poll job.
- Run polling before remediation execution and trigger only bounded repair
  work per cycle.
- Ensure the existing remediation worker is enabled in the deployment that
  should process the backlog.
- Add retry/backoff and circuit-breaker behavior for an unavailable
  Wealthfolio target.

Verification:

- Disabled configuration schedules no bridge job.
- Enabled configuration polls only active Wealthfolio targets.
- A failing target does not block other tenants or targets.
- Worker logs show poll, enqueue, repair, and verification outcomes.

### 6. Add control-plane API and observability

Relevant files:

- `src/finance_sync/api/v1/control_plane.py`
- `src/finance_sync/schemas/data_health.py`
- `src/finance_sync/observability/metrics.py`
- dashboard/control-plane templates where applicable

Tasks:

- Add a bounded manual trigger, for example:
  `POST /api/v1/control-plane/wealthfolio/health-sync`.
- Extend Data Health with bridge status, last successful poll, target count,
  imported issues, resolved issues, and last error.
- Add metrics for polls, failures, imported issues, resolutions, repair
  attempts, manual review, and duration.
- Add permission checks and tenant scoping.

Verification:

- Read-only users cannot trigger polling.
- Manual trigger is bounded and idempotent.
- Metrics contain no target passwords or API keys.

### 7. Test end-to-end and deploy safely

Relevant files:

- `tests/`
- `docker-compose*.yml`
- `coolify.yaml`
- deployment documentation

Tasks:

- Add unit tests for parsing, deduplication, issue lifecycle, and strategy
  routing.
- Add integration tests with mocked Wealthfolio and database fixtures.
- Add an end-to-end test for quote failure → backlog → repair → Wealthfolio
  verification.
- Deploy with bridge disabled, run migrations, then enable polling only.
- Observe backlog status and worker attempts before enabling automatic repair.

Exit criteria:

- Wealthfolio health issues appear in the finance-sync backlog.
- Repeated polling remains idempotent.
- Safe quote and historical-price issues resolve automatically.
- Unsafe issues become actionable `manual_review` items.
- No financial data is invented or deleted.
- Existing exporter and remediation tests remain green.

## Suggested implementation order

1. Client contract and fixtures.
2. Cursor migration and normalized issue model.
3. Poller and backlog enqueue.
4. Shared quote projection service.
5. Safe remediation strategies and verification.
6. Scheduler/configuration.
7. Control-plane API, metrics, and UI.
8. Integration tests and staged deployment.

## Risks and mitigations

- **Wealthfolio API changes:** use tolerant parsing, adapter tests, and
  fail-closed manual review.
- **Duplicate repairs:** stable deduplication plus cursor/hash state.
- **Stale health data:** never resolve after a failed poll; verify remotely.
- **Unsafe financial mutation:** only quote enrichment is automatic initially.
- **Worker disabled in production:** add deployment smoke checks for scheduler
  registration and backlog attempt counts.
- **Provider quote unavailable:** retain the backlog item with bounded retry
  and expose the underlying provider error without leaking credentials.
