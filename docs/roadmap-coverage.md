# Roadmap Coverage Matrix

Audit of the finance-sync codebase against `docs/ROADMAP.md`, indexed as
`docs/roadmap-index.json` (69 entries). Every roadmap item below is judged
**DONE** / **PARTIAL** / **MISSING** with code references.

- **Audit date:** 2026-08-14
- **Audited commit:** `967d1b3` (merge of #193, current main)
- **Repo:** https://github.com/rbnbrls/finance-sync
- **Method:** clone at HEAD; grep/source inspection of `src/`, `tests/`,
  `.github/workflows/`, `migrations/`, `docs/`; GitHub labels checked via
  REST API.
- **ID scheme:** `dr.N` delivery rule · `ms.N` milestone · `ms.N.f.M` feature ·
  `ms.N.ac.M` acceptance criterion · `lb.N` label rule · `tc.N` testing/CI ·
  `rk.N` risk.

## Summary

58 items assessed (33 milestone features + 6 milestone acceptance criteria +
3 delivery rules + 3 label rules + 6 testing/CI items + 7 risks):

| Status | Count |
|---|---|
| DONE | 38 |
| PARTIAL | 17 |
| MISSING | 3 |

(Index `roadmap-index.json` contains 69 entries; the extra 11 are grouping
nodes — 6 milestone groups `ms.N` and 5 structural entries — which carry no
independent acceptance semantics.)

Gaps are consolidated in the **Gap Register** at the bottom; each gap maps to
one or more roadmap IDs and carries a proposed minimal task outline suitable
for direct Kanban task creation.

---

## Delivery rules (dr)

| ID | Item | Status | Evidence |
|---|---|---|---|
| dr.1 | Every milestone ships migrations, typed configuration, structured logs, health signals, tests, documentation, and an upgrade note | **PARTIAL** | Migrations exist (`migrations/versions/0001..0004*`) but the chain is broken (4 files all declare `revision="0004"`, `down_revision="0003"` — duplicate IDs, unresolvable by `alembic upgrade head`); `export_runs`/`export_deliveries` tables have **no migration** at all (only `Base.metadata.create_all` fallback in `src/finance_sync/lifespan.py:48`, which also stamps `ALEMBIC_HEAD="0003"`). Typed config ✓ (`config/settings.py`, pydantic-settings). Structured logs ✓ (`observability/logging.py`, structlog). Health ✓ (`observability/health.py`). Tests ✓ (2,240 `def test`). Docs ✓ (`docs/`). **Upgrade note: missing** — no UPGRADE.md / release notes / changelog anywhere in the repo. → G-01, G-02 |
| dr.2 | Do not begin live connector work until mock/recorded contract fixtures and secret handling exist | **DONE** | Contract fixtures: `tests/connectors/fixtures/{bunq_api,trading212_api,ynab_api}_fixtures.py`, `tests/connectors/contract_test_template.py`, `tests/exporter/contract_test_template.py`. Secret handling: envelope encryption AES-256-GCM (`services/auth.py:115-175`, `MASTER_ENCRYPTION_KEY`), `models/credential.py`. |
| dr.3 | Feature flags protect unfinished providers/exporters | **DONE** | Flags exist for worker jobs and AI/HA: `worker_job_bunq_sync_enabled`, `worker_job_trading212_sync_enabled`, `worker_job_price_enrichment_enabled`, `worker_job_reconciliation_enabled`, `worker_job_outbox_enabled`, `ai_enabled`, `ha_enabled` (`config/settings.py:260-429`, honoured in `worker/scheduler.py` and `api/v1/ai_summary.py`). Per-exporter flags `exporter_actual_budget_enabled` / `exporter_wealthfolio_enabled` added (G-13, PR #202), honoured in `api/v1/exporters.py` (404 when off, type listing filtered) and `cli.py` (exit 2 when off). |

## Milestone 1 — Foundation

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.1.f.1 | pyproject/lint/type/test tooling | **DONE** | `pyproject.toml` (ruff select incl. B/SIM/PERF, pyright, pytest-asyncio, pytest-cov, xdist), `.pre-commit` config, CI lint/types/test jobs. |
| ms.1.f.2 | FastAPI app/settings | **DONE** | `app.py` (create_app factory), `config/settings.py` (typed pydantic-settings, SecretStr, env aliases), `config/environments.py`. |
| ms.1.f.3 | Postgres/Redis/Docker/Coolify | **DONE** | `docker-compose.yml` (postgres:16, redis:7, app, worker, prometheus, grafana), `Dockerfile` (multi-stage, non-root), `coolify.yaml` (compose stack + env reference). |
| ms.1.f.4 | Alembic core schema | **PARTIAL** | `migrations/versions/` 7 files, `migrations/env.py`, `alembic.ini`. Broken chain + unmigrated export tables (see dr.1). Schema actually applied via `create_all` in `lifespan.py`. → G-01 |
| ms.1.f.5 | JWT/API keys/RBAC | **DONE** | `services/auth.py` (JWT encode/decode, bcrypt, API-key create/verify, envelope encryption), `models/api_key.py`, `models/user.py` (`role`), `api/deps/auth.py` (`require_permission(resource, action)`, `require_role`, `get_auth_context`), auth API (`api/v1/auth.py`: register/login/refresh/me/api-keys). |
| ms.1.f.6 | health/metrics/logging | **DONE** | `observability/health.py` (`/health`, `/health/ready`, `/health/live`), `observability/metrics.py` (Prometheus + `/metrics`), `observability/logging.py` (structlog + RequestLogMiddleware), `worker/health.py` (aiohttp on :9090). |
| ms.1.f.7 | CI | **DONE** | `.github/workflows/ci.yml` (lint, types, test, security, build & push, deploy), `ci-failure.yml` (issue on failure), `deploy.yml`. |
| ms.1.ac.1 | A fresh deployment migrates, authenticates, exposes readiness | **PARTIAL** | Works in practice via `create_all` + seed tenant/admin (`lifespan.py:33-110`), auth + health endpoints present. But `alembic upgrade head` cannot be the schema path (broken chain). → G-01 |

## Milestone 2 — Ingestion

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.2.f.1 | connector SDK/registry | **DONE** | `connectors/registry.py` (entry-point discovery `finance_sync.connectors`), `connectors/base.py` (abstract Connector, `sdk_version`, rate-limit policy), `connectors/rate_limiter.py`, `connectors/models.py` (raw + canonical DTOs), `connectors/exceptions.py` (Permanent/Transient), entry points in `pyproject.toml:57-64`. SDK package `sdks/finance-sync-sdk/`. |
| ms.2.f.2 | sync-run/cursor/outbox | **PARTIAL** | SyncRun ✓ (`models/sync_run.py`, `sync/sync_run.py`, `api/v1/sync_runs.py`). Outbox ✓ (`models/outbox.py`, `sync/outbox.py`, `sync/outbox_publisher.py` with idempotency keys + polling worker job `process_outbox_job`). **Cursor: missing** — `SyncRun` has no cursor/watermark column; syncs use a `since` parameter (default 90 days, `sync/orchestrator.py:140`); `docs/DATABASE.md` documents a `sync_cursor` table that does not exist. → G-03 |
| ms.2.f.3 | canonical accounts/transactions/portfolio schema | **DONE** | `models/account.py`, `models/transaction.py` (unique `uq_transactions_provider` on tenant+provider+external id, `provider_fingerprint`), `models/holding.py`, `models/balance.py`, `migrations/versions/0001_initial_schema.py`. |
| ms.2.f.4 | bunq accounts/balances/transactions | **DONE** | `connectors/bunq.py` (session-server auth, accounts, payments, balances), `tests/connectors/bunq/test_bunq_connector.py`, `tests/connectors/fixtures/bunq_api_fixtures.py`. |
| ms.2.f.5 | Trading212 portfolio/holdings/cash/orders/dividends | **DONE** | `connectors/trading212.py` (`fetch_portfolio`, orders, transactions incl. DIVIDEND type, cursor pagination), `tests/connectors/trading212/test_trading212_connector.py`, fixtures. |
| ms.2.f.6 | scheduled payments/cards | **PARTIAL** | Models + schema exist: `models/scheduled_payment.py`, `models/card_transaction.py`, tables in `migrations/versions/0004_add_phase3_tables.py` (sections 7-8). bunq connector can fetch them: `fetch_scheduled_payments()`, `fetch_card_transactions()` (`connectors/bunq.py:368,490`), test `tests/connectors/bunq/test_bunq_scheduled_cards.py`. **Not wired into sync pipeline** (orchestrator/worker never call these), **no API endpoints**, no worker job. → G-04 |
| ms.2.f.7 | reconciliation | **DONE** | `services/reconciliation.py` (duplicate + cross-connector gap detection), `sync/reconciliation.py`, `sync/orchestrator.py` (post-sync auto-reconcile, flag-gated), `api/v1/reconciliation.py`, CLI (`cli.py`, `docs/API.md` CLI section), `tests/test_reconciliation*.py` (7 files), worker job `nightly_reconciliation_job`. |
| ms.2.ac.1 | Re-running a sync produces no duplicate facts/events | **DONE** | Upsert by `(tenant_id, provider_key, external_*_id)` in `sync/orchestrator.py` (`_upsert_account`/`_upsert_transaction`), DB unique constraints (`uq_transactions_provider`), outbox idempotency keys (`sync/outbox.py`), `provider_fingerprint` on transactions, `tests/test_sync_orchestrator.py` pipeline tests, `tests/test_outbox.py`. |

## Milestone 3 — Enrichment

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.3.f.1 | security/listing resolver | **DONE** | `identity/resolver.py` (4-stage: exact ISIN → FIGI/ticker → fuzzy name → manual queue; cleansing rules, confidence scores, audit log), `enrichment/security_resolver.py`, `models/security_listing.py`, `models/unresolved_security.py`, `models/resolution_audit_log.py`, `api/v1/securities.py` (unresolved/resolve/map/audit-log), `tests/identity/`. |
| ms.3.f.2 | OpenBB gateway and cache policy | **DONE** | `enrichment/gateway.py` (degraded mode without key, rate limits, httpx client), `enrichment/price_store.py` (dedupe, prune), `config/settings.py` OpenBB block (base URL, API version, `openbb_rate_limit_rps`), `tests/enrichment/test_gateway.py`. |
| ms.3.f.3 | latest/historical prices | **DONE** | `enrichment/gateway.py` (`get_latest_quote`, `get_historical_prices` with local-cache-first), `enrichment/price_store.py`, `api/v1/securities.py` `GET /securities/{id}/prices`, `tests/enrichment/test_price_store.py`. |
| ms.3.f.4 | fundamentals/ETF metadata | **DONE** | `enrichment/gateway.py` (`get_fundamentals`, `get_etf_composition`), `enrichment/metadata_enricher.py`, `models/fundamental_observation.py`, `models/security_metadata_observation.py`, migration `0004_add_fundamentals_metadata_tables.py`, `tests/enrichment/test_fundamentals_enrichment.py`. |
| ms.3.f.5 | FX valuation | **DONE** | `services/fx_service.py` (cache + history + graceful degradation), `providers/openbb_fx.py`, `models/fx_rate.py`, `schemas/fx_rate.py`, `utils/currency_converter.py`, migration `0004_add_fx_rates.py`, `tests/test_fx_service.py`, `tests/test_openbb_fx_provider.py`, `tests/test_fx_rate_model.py`, `tests/test_currency_converter.py`. |
| ms.3.ac.1 | Cached data honors TTL and records provenance/freshness | **DONE** | Freshness/provenance: `models/enrichment_freshness.py`, `gateway.update_freshness()`, `api/v1/enrichment.py` (`/enrichment/status` with stale counts), price `source` field, `PriceStore.prune_*`. TTL enforced on cache reads: `price_cache_ttl_seconds` (`config/settings.py`), `get_historical_prices` refetches when the newest cached row is older than the TTL or shorter than `limit`, `get_latest_quote` flags local fallbacks older than the TTL with `stale=True`; failed refetches serve the cache with an explicit stale flag (`enrichment/gateway.py`, `enrichment/models.py` `PriceHistoryResult`/`QuoteResult.stale`). → G-07 (resolved) |

## Milestone 4 — Consumer API

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.4.f.1 | read REST endpoints/OpenAPI | **DONE** | 83 operations implemented (`api/v1/`, 75 paths): accounts, transactions, holdings, dividends, prices, portfolio, allocation, cashflow, net-worth, performance, subscriptions, securities, sync-runs, sync, reconciliation, exporters, connectors, ai, webhooks; FastAPI auto OpenAPI at `/docs` + `/openapi.json`. Top-level `GET /transactions`, `GET /holdings`, `GET /dividends`, `GET /prices`, `POST /sync` (+ `POST /sync/{provider}`) implemented with the `meta:{asOf,currency,nextCursor,freshness}` envelope (`schemas/freshness.py` `CollectionMeta`), matching `docs/API.md`. → G-05 (resolved) |
| ms.4.f.2 | portfolio, allocation, cashflow/net-worth services | **DONE** | `services/read_api.py` (portfolio + history, net-worth + history), `services/allocation.py`, `services/cashflow.py`, `api/v1/{portfolio,allocation,cashflow,net_worth}.py`, `tests/test_allocation.py`, `tests/test_cashflow.py`, `tests/test_read_api.py`. |
| ms.4.f.3 | Actual Budget exporter | **DONE** | `exporter/actual_budget/` (client, config, exporter with ExportRun + ExportDelivery cursor, transaction_mapper, models), `api/v1/exporters.py` (types/config/runs), `tests/test_actual_budget_exporter.py`, `tests/exporter/test_actual_budget_contract.py`. |
| ms.4.f.4 | Wealthfolio exporter | **DONE** | `exporter/wealthfolio/` (client, config, exporter, transaction_mapper, models), API wiring in `api/v1/exporters.py`, `tests/test_wealthfolio_exporter.py`, `tests/exporter/test_wealthfolio_client.py`, `tests/exporter/test_wealthfolio_contract.py`. |
| ms.4.f.5 | exporter contract suites | **DONE** | `tests/exporter/contract_test_template.py` (config/result/mapping/lifecycle/CSV mixins) + concrete suites for both exporters. |
| ms.4.ac.1 | Consumer failure retries without source data loss | **PARTIAL** | Actual Budget: ExportRun + per-account `ExportDelivery` cursor for idempotent resume (`exporter/actual_budget/exporter.py:246,460-471`), `retry_with_backoff` in worker. **Gaps**: export tables have no Alembic migration (created only via `create_all`); no dead-letter visibility for failed exports; Wealthfolio exporter has no delivery cursor. → G-14 |

## Milestone 5 — Automation/insights

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.5.f.1 | AI summary endpoints | **DONE** | `api/v1/ai_summary.py` (`POST /ai/summary`, `POST /ai/summary/daily`), `services/ai_summary.py` (OpenAI/Anthropic prompt templates, 1h cache, rate limit), `api/middleware/ai_rate_limit.py`, gated by `ai_enabled`, covered in `tests/test_phase52.py`, MCP tool `tool_get_summary`/`tool_get_daily_briefing`. |
| ms.5.f.2 | Home Assistant pull integration | **DONE** | `api/v1/ha_integration.py` (`GET /ha/sensors`, `GET /ha/config`), `services/ha_integration.py` (REST-sensor payloads: net worth, portfolio, last sync), gated by `ha_enabled`, covered in `tests/test_phase52.py`. |
| ms.5.f.3 | Grafana dashboard/alerts | **DONE** | Dashboards ✓: `docker/grafana/dashboards/{portfolio,system,sync-health}.json` + provisioning + `docker/prometheus.yml`, compose service. Alert rules ✓ (G-06): file-provisioned via `docker/grafana/provisioning/alerting/` (`finance-sync.rules.yaml` + `alerting.yaml`), covering failed sync runs, stale enrichment (>24h), outbox backlog, export failures, worker/app down; channels documented in `docs/observability.md`; missing metrics instrumented (outbox gauge, enrichment staleness, export counters, worker `/metrics`). |
| ms.5.f.4 | performance analytics | **DONE** | `api/v1/performance.py` (summary, TWR, MWR, benchmark, attribution), `services/performance.py` (IRR iteration, Brinson attribution), `tests/test_performance.py`. |
| ms.5.f.5 | subscription detection | **DONE** | `services/subscription_detector/` (merchant classifier, pattern detector, service with HYBRID cross-validation), `api/v1/subscriptions.py` (detect/analyze/confirm/ignore), docs `docs/subscription-detection.md`, 8 test files (incl. deep-coverage + edge cases). |
| ms.5.ac.1 | Every aggregate declares as-of/freshness/coverage | **DONE** | Portfolio/net-worth carry `as_of` (`services/read_api.py`); `/enrichment/status` reports coverage/staleness. Allocation, cashflow, performance, and subscriptions responses now declare the `meta` envelope (`schemas/freshness.py` `AggregateMeta`/`CoverageInfo`, wired in `services/allocation.py`, `services/read_api.py`, `services/performance.py`, `api/v1/subscriptions.py`), documented in `docs/API.md`. → G-07 (resolved) |

## Milestone 6 — Ecosystem

| ID | Feature | Status | Evidence |
|---|---|---|---|
| ms.6.f.1 | versioned plugin SDK and compatibility policy | **DONE** (roadmap ✅) | `sdks/finance-sync-sdk/` (plugin.py, registry.py, credentials.py, rate_limiter.py, models, exceptions, config), `COMPATIBILITY.md` (SemVer + 0.x policy), `py.typed`, `publish-sdk.yml` (PyPI on `sdk-v*` tag), `docs/plugin-development.md`, `docs/connector-api.md`, `tests/test_plugin_integration.py` + SDK tests. |
| ms.6.f.2 | MCP server | **DONE** | `mcp/server.py` (FastMCP, SSE + stdio, 4 resources + 11 tools), `mcp/auth.py` (JWT/API-key middleware), `mcp/__main__.py`, `docs/MCP.md`, `docs/mcp-server.md`, `tests/test_mcp_server.py` (20), `tests/test_mcp_integration.py` (21). |
| ms.6.f.3 | additional connectors | **DONE** | Beyond bunq/Trading212: `connectors/ynab.py`, `connectors/csv_import.py`, `connectors/manual_expense.py`, `connectors/plaid_like.py` + per-connector tests and `docs/connectors-overview.md`. |
| ms.6.f.4 | tax lots/calculations | **DONE** | `services/tax_lot_service.py` (FIFO lot accounting), `api/v1/tax_lots.py` (list/summary/compute), `models/tax_lot.py`, migration `0004_add_tax_lots.py`, `tests/test_tax_lots.py` (23 tests). |
| ms.6.ac.1 | Third-party plugin installable/configured without core source edits | **DONE** | Entry-point discovery (`connectors/registry.py`, SDK `PluginRegistry`), `sdks/finance-sync-sdk` package, `docs/plugin-development.md` + `docs/connector-api.md` + `examples/`, `tests/test_plugin_integration.py`. |

## GitHub labels and issue ordering (lb)

| ID | Item | Status | Evidence |
|---|---|---|---|
| lb.1 | Use labels `area:api`, `area:connector`, `area:data`, `area:enrichment`, `area:exporter`, `area:ops`, `security`, `good-first-issue`, `blocked:provider`, `priority:P0/P1/P2` | **MISSING** | GitHub API label list for `rbnbrls/finance-sync`: `bug, ci-failure, coolify-error, documentation, duplicate, enhancement, feedback, good first issue, help wanted, hermes-auto, invalid, needs-review, priority:high, question, roadmap, scope:pr, scope:push, test, type:build-failure, type:lint, type:test-failure, wontfix`. **None of the roadmap labels exist** (no `area:*`, no `security`, no `blocked:provider`, no `priority:P0/P1/P2`). → G-08 |
| lb.2 | Close milestones only after automated acceptance tests against PostgreSQL/Redis and recorded provider fixtures | **PARTIAL** | Recorded provider fixtures exist (`tests/connectors/fixtures/*`). But no automated acceptance suite runs against real PostgreSQL/Redis — tests use aiosqlite in-memory (see tc.2). → G-09 |
| lb.3 | Provider secrets and real account data never prerequisites for ordinary CI | **DONE** | CI test job requires no provider secrets; only `GITHUB_TOKEN` (image push) and `COOLIFY_API_TOKEN` (deploy) appear in `ci.yml`. |

## Testing and CI/CD (tc)

| ID | Item | Status | Evidence |
|---|---|---|---|
| tc.1 | Unit tests: domain policies/mappers with no I/O | **DONE** | 2,240 tests; pure domain suites: `test_models.py`, `test_enums.py`, `test_types.py`, `test_transaction_mapper.py`, `test_merchant_classifier*.py`, `test_pattern_detector*.py`, `test_tax_lots.py`, `test_allocation.py`, `test_cashflow.py`, connector contract tests. |
| tc.2 | Integration tests: async SQLAlchemy repos, migrations, Redis locks/rate limits, outbox, with ephemeral PostgreSQL/Redis containers | **MISSING** | No testcontainers / docker-based test harness anywhere; `tests/conftest.py` and DB-backed tests use `aiosqlite` (e.g. `tests/test_outbox.py`, `tests/test_sync_orchestrator.py`, `tests/test_repository.py`). No migration-upgrade test. No Redis integration test. → G-09 |
| tc.3 | Contract tests: connectors/exporters against fixtures; live tests opt-in and secret-gated | **PARTIAL** | Contract templates + fixture suites exist (`tests/connectors/contract_test_template.py`, `tests/exporter/contract_test_template.py`, fixture packages). **No live-test opt-in / secret-gating mechanism** (no `LIVE=1`-style gate or env-gated live suite). → G-12 |
| tc.4 | E2E tests: API-to-worker flow verifies exactly-once observable outcome after at-least-once delivery | **MISSING** | Zero e2e/playwright tests; `e2e` marker defined in `pyproject.toml` but unused; no API→worker E2E harness. → G-10 |
| tc.5 | Quality gates: Ruff, pyright strict for app/, coverage ≥85% ratcheted, dependency/SBOM/image scans, OpenAPI diff, migration upgrade test | **PARTIAL** | Ruff ✓ (lint+format jobs). Pyright ✗/partial: config is `typeCheckingMode = "basic"` (`pyproject.toml:143`) while CI labels the job "Pyright (strict)" — not strict for `app/`. Coverage ✗: `fail_under = 70` in `pyproject.toml:175` and CI passes `--cov-fail-under=70` — roadmap requires 85% minimum (README even claims 85%). Dep/SBOM scans ✓ (`pip-audit` + CycloneDX in security job). **Image scan missing** (no trivy/grype on built image). **OpenAPI diff missing.** **Migration upgrade test missing.** → G-11 |
| tc.6 | CI PR checks + protected release pipeline (immutable tags, scan/sign, migration job, staging stack, Coolify promotion; rollback = image rollback + backward-compatible migrations) | **PARTIAL** | PR checks ✓. Immutable sha tags ✓ (`type=sha` + `latest` in ci.yml build). SBOM scan ✓ (source deps only). **No image signing** (no cosign). **No migration job** in release. **No staging stack / promotion** — `deploy` job calls Coolify directly on main push. Rollback strategy documented only in prose (README/coolify.yaml). → G-12 |

## Risks and mitigations (rk) — mitigation status

| ID | Risk | Mitigation status | Evidence |
|---|---|---|---|
| rk.1 | Provider APIs change/limit/omit history | **DONE** | `sdk_version` + connector versioning (`connectors/base.py`), fixture suites, `rate_limit_policy` + backoff retries (`connectors/rate_limiter.py`, `worker/jobs.py:retry_with_backoff`), manual sync + reconciliation API/CLI. |
| rk.2 | Ambiguous ticker identity | **DONE** | ISIN/FIGI-first 4-stage resolver (`identity/resolver.py`), `security_listings` model, confidence scores, unresolved review queue + audit log + resolve/map APIs. |
| rk.3 | Duplicate/mutating transactions | **DONE** | Unique `(tenant, provider, external_id)` + `provider_fingerprint` (`models/transaction.py`), upsert semantics in orchestrator, outbox idempotency keys, reconciliation duplicate detection. |
| rk.4 | Stale/incomplete valuation | **DONE** | Per-field freshness (`models/enrichment_freshness.py`), price provenance (`source`), coverage endpoint, TTL enforced on cache reads with explicit stale flags, and per-aggregate `meta` coverage/caveats (see ms.3.ac.1 / ms.5.ac.1). → G-07 (resolved) |
| rk.5 | Credential/financial-data exposure | **DONE** | Envelope encryption AES-256-GCM (`services/auth.py`), scoped API-key permissions + RBAC (`api/deps/auth.py`), resolution audit logs, pip-audit + SBOM in CI, non-root Docker user. |
| rk.6 | Exporter API mismatch | **PARTIAL** | Isolated adapters (`exporter/actual_budget/`, `exporter/wealthfolio/`), integration contract tests ✓, delivery cursor ✓ (Actual Budget only). **Dead-letter visibility missing**; Wealthfolio lacks cursor; export tables unmigrated. → G-14 |
| rk.7 | Premature distributed complexity | **DONE** | Modular monolith documented (`docs/ARCHITECTURE.md`, ADR-0001 `docs/adr/0001-modular-monolith-and-durable-outbox.md`); single compose stack, extractable boundaries. |

---

## Gap Register

Each gap lists the roadmap IDs it closes, what is missing, and a minimal
task outline (title / scope / acceptance criteria) ready for downstream task
creation. **None of these tasks may depend on Hermes cron or Hermes-managed
scheduling** — scheduling must live in the repo (APScheduler worker, GitHub
Actions, or system cron inside the deployment).

### G-01 — Repair Alembic migration chain and migrate export tables
- **Roadmap IDs:** dr.1, ms.1.f.4, ms.1.ac.1, tc.5
- **What's missing:** Four migration files declare `revision="0004"` with
  `down_revision="0003"` (`0004_add_fundamentals_metadata_tables`,
  `0004_add_fx_rates`, `0004_add_phase3_tables`, `0004_add_tax_lots`) —
  duplicate revision IDs make `alembic upgrade head` ambiguous/broken.
  `export_runs` and `export_deliveries` (ORM models
  `exporter/models.py`, `exporter/actual_budget/models.py`) have no
  migration; `lifespan.py` sidesteps Alembic with `create_all` and stamps
  head `"0003"`.
- **Task outline:**
  - Title: `Fix Alembic migration chain (unique revisions, export tables)`
  - Scope: renumber 0004-family revisions into a linear chain (0004, 0005,
    0006, 0007); add `export_runs` + `export_deliveries` migration; remove
    `create_all` schema fallback from `lifespan.py` (keep seed); add
    `alembic upgrade head` to CI as a migration upgrade test; document
    migration workflow in `docs/`.
  - Acceptance: `alembic upgrade head` runs clean on an empty PG database
    and `alembic history` shows one linear chain; app boots without
    `create_all`; CI runs an upgrade test.

### G-02 — Add per-milestone upgrade notes
- **Roadmap IDs:** dr.1
- **What's missing:** No UPGRADE.md / release notes / changelog.
- **Task outline:**
  - Title: `Add docs/UPGRADE.md with per-milestone upgrade notes`
  - Scope: create UPGRADE.md covering schema changes per migration,
    breaking config changes, and rollback notes; reference from README.
  - Acceptance: every migration/breaking change since 0001 has an upgrade
    note; README links it.

### G-03 — Persist sync cursor/watermark per connector
- **Roadmap IDs:** ms.2.f.2, ms.2.ac.1 (incremental sync)
- **What's missing:** No cursor storage; `SyncRun` lacks cursor fields and
  `docs/DATABASE.md`'s `sync_cursor` table doesn't exist. Orchestrator
  always defaults to `since = now - 90 days`.
- **Task outline:**
  - Title: `Add sync cursor persistence to ingestion pipeline`
  - Scope: add cursor/watermark columns (or `sync_cursor` table:
    connector, resource, cursor, updated_at); orchestrator reads last
    cursor per account and writes it on success; expose cursor in
    `GET /sync-runs` response; keep the 90-day default for first sync.
  - Acceptance: re-running a sync resumes from the stored cursor; cursor
    visible via API; unit/integration tests cover first-sync and
    resume paths.

### G-04 — Wire scheduled payments/cards into sync + expose API
- **Roadmap IDs:** ms.2.f.6
- **What's missing:** Models, tables, and bunq fetch methods exist, but the
  orchestrator/worker never fetch scheduled payments or card transactions
  and there are no API endpoints to read them.
- **Task outline:**
  - Title: `Sync scheduled payments and card transactions (bunq) + read API`
  - Scope: extend `SyncOrchestrator` (or a dedicated job) to call
    `fetch_scheduled_payments`/`fetch_card_transactions` and upsert
    `ScheduledPayment`/`CardTransaction`; add read endpoints
    (`GET /scheduled-payments`, `GET /card-transactions` with account
    filters); add feature flag `worker_job_bunq_cards_enabled` (dr.3);
    extend bunq fixtures + tests.
  - Acceptance: sync run ingests scheduled payments + card transactions
    idempotently; both endpoints return persisted data; flag-gated;
    tests green.

### G-05 — Align API surface with docs/API.md
- **Roadmap IDs:** ms.4.f.1
- **Status:** RESOLVED — top-level `GET /transactions`, `GET /holdings`,
  `GET /dividends`, `GET /prices`, `POST /sync` (and `POST /sync/{provider}`)
  implemented in `api/v1/{transactions,holdings,dividends,prices,sync}.py`,
  backed by `ReadService`/`SyncOrchestrator`, each returning the
  `meta:{asOf,currency,nextCursor,freshness}` envelope; `docs/API.md` rows
  updated to match the implemented filters; covered by
  `tests/test_top_level_read_endpoints.py`.
- **What's missing:** `docs/API.md` documents top-level `GET /transactions`,
  `GET /holdings`, `GET /dividends`, `GET /prices`, `POST /sync` that don't
  exist; implementation has account-scoped equivalents only.
- **Task outline (choose A or B, do not do both blindly):**
  - Title: `Implement documented top-level read endpoints (transactions/holdings/dividends/prices/sync)`
  - Scope: add top-level routers backed by `ReadService`/`SyncOrchestrator`
    with `meta:{asOf,currency,nextCursor,freshness}` envelopes per API.md;
    OR trim API.md to the implemented surface and add a doc-vs-openapi
    consistency check. Add OpenAPI diff gate (see G-11).
  - Acceptance: `GET /transactions`, `GET /holdings`, `GET /dividends`,
    `GET /prices`, `POST /sync` exist and return documented shapes; or
    API.md matches `/openapi.json` exactly.

### G-06 — Add Grafana alert rules
- **Roadmap IDs:** ms.5.f.3, ms.5.ac.1
- **Status:** DONE (merged PR #207, 2026-08-14)
- **What's missing:** Dashboards exist but no alerts (sync health, stale
  enrichment, failed exports, outbox lag all unmonitored).
- **Task outline:**
  - Title: `Add Grafana alert rules for sync/enrichment/export health`
  - Scope: add alert rules (provisioned via
    `docker/grafana/provisioning/`) on existing metrics
    (`sync_runs_total`, `transactions_ingested_total`, outbox lag, worker
    health) for: failed sync runs, stale enrichment (>24h), outbox backlog,
    export run failures; document alert channels.
  - Acceptance: alerts defined in provisioning config, referenced by
    dashboards, and documented; compose stack loads them.
- **Resolution:** file-provisioned rules in
  `docker/grafana/provisioning/alerting/finance-sync.rules.yaml` (6 rules)
  + `alerting.yaml` (webhook/email contact points, policy, mute timing);
  dashboard panels link to their rules; `docs/observability.md` documents
  inventory/channels/silencing; instrumented the previously-missing
  metrics (outbox gauge, enrichment staleness, export counters, worker
  job gauges) and added the worker `/metrics` route; compose mounts the
  alerting provisioning dir.

### G-07 — Enforce cache TTL and per-aggregate freshness/coverage
- **Roadmap IDs:** ms.3.ac.1, ms.5.ac.1, rk.4
- **Status:** RESOLVED — price cache reads honor `price_cache_ttl_seconds`
  (refetch when stale, explicit stale flag when the source is down);
  allocation/cashflow/performance/subscriptions declare the `meta`
  envelope (`schemas/freshness.py`), exposed via OpenAPI and documented
  in `docs/API.md`.
- **What was missing:** Price cache reads don't honor TTL; most aggregate
  endpoints lack as-of/freshness/coverage metadata.
- **Task outline:**
  - Title: `Enforce price TTL on cache reads and add freshness metadata to aggregates`
  - Scope: add freshness/TTL policy to `PriceStore`/`gateway` reads
    (return stale marker or refetch when older than configured TTL); add
    `asOf`/`freshness`/`coverage` to allocation, cashflow, performance,
    subscriptions responses; expose in OpenAPI schemas.
  - Acceptance: cached prices older than TTL are refetched (or flagged
    stale); every aggregate response declares as-of/freshness/coverage;
    tests cover stale-cache behavior.

### G-08 — Apply roadmap label taxonomy to GitHub repo
- **Roadmap IDs:** lb.1
- **What's missing:** None of `area:*`, `security`, `good-first-issue`,
  `blocked:provider`, `priority:P0/P1/P2` exist as repo labels.
- **Task outline:**
  - Title: `Create roadmap label set in rbnbrls/finance-sync`
  - Scope: create labels via GitHub API: `area:api`, `area:connector`,
    `area:data`, `area:enrichment`, `area:exporter`, `area:ops`,
    `security`, `good-first-issue`, `blocked:provider`,
    `priority:P0`, `priority:P1`, `priority:P2`.
  - Acceptance: `GET /repos/rbnbrls/finance-sync/labels` returns the full
    set; `docs/ROADMAP.md` label paragraph matches.

### G-09 — Integration test harness with ephemeral PostgreSQL/Redis
- **Roadmap IDs:** tc.2, lb.2, ms.1.ac.1
- **What's missing:** No containerized integration tests; DB tests run on
  aiosqlite; no migration-upgrade test; no Redis lock/rate-limit/outbox
  integration tests.
- **Task outline:**
  - Title: `Add ephemeral PostgreSQL/Redis integration test suite`
  - Scope: add testcontainers (or compose-based fixture) running real PG +
    Redis; port key integration tests (repositories, outbox, sync
    orchestrator, rate limiter, migrations) to it; add
    `alembic upgrade head` test; keep aiosqlite suite for fast unit runs.
  - Acceptance: CI job `integration` starts ephemeral PG+Redis, runs the
    ported suite green, and runs a migration-upgrade test; documented in
    README.

### G-10 — E2E exactly-once test (API → worker)
- **Roadmap IDs:** tc.4, ms.2.ac.1
- **What's missing:** No e2e tests; `e2e` pytest marker is unused.
- **Task outline:**
  - Title: `Add API-to-worker E2E test proving exactly-once outcome`
  - Scope: bring up app+worker+PG+Redis (compose or testcontainers); drive
    a sync via API, force a worker redelivery (at-least-once), assert
    observable outcome (transactions/outbox/export) is exactly-once;
    mark tests `@pytest.mark.e2e`.
  - Acceptance: e2e suite runs in CI, passes, and documents the
    at-least-once → exactly-once assertion.

### G-11 — Raise quality gates to roadmap spec
- **Roadmap IDs:** tc.5, dr.1
- **What's missing:** Coverage 70% vs 85% required; pyright `basic` vs
  `strict` for app/; no OpenAPI diff gate; no image vulnerability scan;
  no migration upgrade test (see G-01/G-09).
- **Task outline:**
  - Title: `Raise CI quality gates: 85% coverage, pyright strict, OpenAPI diff, image scan`
  - Scope: ratchet `fail_under` to 85 (incrementally with per-module
    baseline if needed); set pyright `typeCheckingMode="strict"` for
    `src/` (fix violations); add OpenAPI diff job (compare PR openapi.json
    against main); add trivy/grype image scan to build job.
  - Acceptance: CI enforces 85% coverage, strict pyright, OpenAPI diff
    check, and image scan; README CI table updated.

### G-12 — Complete release pipeline (staging, signing, migration job)
- **Roadmap IDs:** tc.6, lb.2
- **What's missing:** Release pipeline = build + push + direct Coolify
  deploy; no staging stack, no image signing, no release migration job, no
  promote step.
- **Task outline:**
  - Title: `Add protected release pipeline: staging stack, signing, migration job, promotion`
  - Scope: add `release.yml` on tags: build immutable tag, cosign sign,
    scan, run `alembic upgrade head` job against staging DB, deploy staging
    stack, run acceptance smoke tests, then promote to production via
    Coolify API; document rollback (image rollback + backward-compatible
    migrations).
  - Acceptance: tag push runs the full pipeline; staging smoke tests gate
    promotion; rollback procedure documented.

### G-13 — Feature-flag exporters
- **Roadmap IDs:** dr.3
- **What's missing:** Exporters have no feature flags; unfinished
  exporter work would ship unconditionally.
- **Task outline:**
  - Title: `Add feature flags for exporters`
  - Scope: add `exporter_actual_budget_enabled` /
    `exporter_wealthfolio_enabled` settings; API returns 404/disabled state
    when flag off; document in `.env.example`.
  - Acceptance: toggling the flag disables/enables each exporter's API and
    worker job without code change.
- **Status: IMPLEMENTED** (PR #202). Both flags default to `true` (matching
  historical behaviour; the Actual Budget R1 CLI landed in PR #201).
  Honoured in `api/v1/exporters.py` (404 on config/export, type listing
  filtered) and `cli.py` (exit 2 on export/push); documented in
  `.env.example` and `docs/API.md`.

### G-14 — Export dead-letter visibility + Wealthfolio delivery cursor + export migrations
- **Roadmap IDs:** ms.4.ac.1, rk.6, dr.1
- **What's missing:** No DLQ visibility for export failures; Wealthfolio
  exporter lacks a delivery cursor; export tables unmigrated (see G-01).
- **Task outline:**
  - Title: `Export resilience: delivery cursor for Wealthfolio, DLQ visibility, migrations`
  - Scope: add `ExportDelivery`-style cursor to Wealthfolio exporter;
    expose failed/retryable export runs with error detail and a retry
    endpoint; ensure export tables covered by Alembic (via G-01).
  - Acceptance: failed export runs are listed with errors and can be
    retried without data loss; Wealthfolio resumes idempotently; tests
    cover retry.

---

## Verification notes

- Clone audited at `967d1b3` (merge of PR #193). Code references are
  paths + symbols at that commit; line numbers may drift on later edits.
- Endpoint inventory from `@router.*` decorators across `api/v1/`.
- Test counts from `grep -c "def test"` across `tests/` (2,240).
- Labels verified live against GitHub API
  (`GET /repos/rbnbrls/finance-sync/labels`).
- The migration-chain defect (duplicate `revision="0004"` values) is a
  factual observation from `migrations/versions/*.py`; the fix (G-01)
  should be validated with `alembic history` before renumbering.
