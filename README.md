# finance-sync

[![CI](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml)

|Self-hosted, API-first persoonlijke financiële datalake. Het importeert providerdata, normaliseert die naar een provider-onafhankelijk ledger- en portfoliomodel, verrijkt securities via OpenBB en kan optioneel consumenten zoals Actual Budget, Wealthfolio en Jupyter-notebooks bedienen.

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD (`.github/workflows/ci.yml`):

| Stage | Description |
|-------|-------------|
| **Lint** | Ruff check + format check |
| **Type check** | Pyright strict for `src/`, basic for `tests/`, plus a versioned source-warning budget (`config/pyright-warning-budget.json`) |
| **Test** | Pytest unit suite (aiosqlite, no external services) with coverage gate ≥ 70% |
| **Migrations** | Alembic linear-chain check + `upgrade head` on an empty PostgreSQL |
| **Integration** | `pytest -m integration` against ephemeral PostgreSQL + Redis (repositories, outbox, sync orchestrator, Redis locks/rate-limits, migration upgrade/downgrade) |
| **E2E** | `pytest -m e2e` — full API → worker → outbox pipeline against ephemeral PostgreSQL + Redis, proving the exactly-once observable outcome under at-least-once delivery |
| **Security** | pip-audit vulnerability scan + CycloneDX SBOM |
| **OpenAPI diff** | PR-only gate: OpenAPI document generated for the PR head and merge base; breaking/non-additive changes fail (see below) |
| **Build & Push** | Docker image built with Buildx, scanned with Trivy (fails on HIGH/CRITICAL findings not in the accepted-risk baseline), pushed to `ghcr.io/rbnbrls/finance-sync` on `main` |
| **Deploy** | Triggers Coolify deployment on push to `main` |
| **Release** | Tag-triggered (`v*`) protected release pipeline: build immutable image (sha tag), scan, cosign-sign, migration job, deploy staging stack, smoke tests that **gate** production promotion (see [Releases](#releases) / `docs/RELEASING.md`) |

### Releases

Releases are **tag-driven** (`.github/workflows/release.yml`).  Pushing a
`v*` tag runs: build (immutable `ghcr.io/rbnbrls/finance-sync:<sha>` +
semver tag) → Trivy scan → cosign keyless sign/verify → migration job
(`alembic upgrade head` on a staging database) → staging stack deploy
(Coolify) → acceptance smoke tests (health, auth, synthetic sync, outbox and
exporter readback) → **promote
to production** via the Coolify API.  The staging smoke tests are the
promotion gate: any failure stops the pipeline before production is
touched.  Manual runs via *Actions → Release → Run workflow*.

Rollback is **image rollback + backward-compatible migrations** — full
runbook in `docs/RELEASING.md`; migration policy in `docs/MIGRATIONS.md` /
`docs/UPGRADE.md`.

The smoke run uses deterministic staging-provider fixtures and uploads
commit/image-tag-bound evidence as `release-staging-smoke-<sha>`. It contains
status and counts only; secrets and financial payloads are excluded.

Release 13 closes with a Pyright source-warning budget of at most 60,
mandatory scan/service gates and a commit/image/artifact/owner/date checklist
in [docs/RELEASING.md](docs/RELEASING.md).

### API contract (OpenAPI) diff gate

Every pull request is checked by an **OpenAPI diff** job.  The job generates
the API's OpenAPI document (`app.openapi()`, served live at `/openapi.json`)
from both the PR head and the merge base, then compares the public surface
with `scripts/check_openapi_diff.py`.  Generation imports the app without
starting the lifespan, so the job needs **no secrets** (no database, no
external services).

The **additive-only policy**: PRs may only extend the API surface.  Anything
additive (new path, new operation, new optional parameter, new optional
property, new schema, new response code/media type, new enum value) is
reported but allowed.  Prohibited changes **fail the job**:

- removed path or removed HTTP method on an existing path
- changed or removed `operationId`
- removed parameter; parameter becoming required; changed parameter
  `in`, type or `$ref`; removed enum value
- removed request body; request body becoming required; removed media type
- removed response status code; removed response media type; changed
  response schema
- removed schema; removed property; property becoming required; newly added
  required property; changed property type
- removed component (security scheme, parameter, request body, response,
  header); added security requirement on an operation

Documentation-only changes (summaries, descriptions, `format`/`default`/
`example`, `info.version`) are informational and never fail.

**Allowlist** — intentional, documented exceptions live in
`scripts/openapi_diff_allowlist.json` as a mapping of finding signature to
reason (signatures are printed by the checker, e.g.
`removed_path:DELETE /api/v1/legacy-endpoint`).  Stale allowlist entries
that no longer match any finding fail the job, so the allowlist cannot rot.

Run locally:

```bash
uv run python scripts/generate_openapi.py --output /tmp/openapi-head.json
uv run python scripts/check_openapi_diff.py --base /tmp/openapi-base.json --head /tmp/openapi-head.json
```

### Image vulnerability scan

Every build is scanned with **Trivy** before anything is pushed.  The
`Build & Push` job builds the image locally (`finance-sync:scan`), scans it
at severity **HIGH/CRITICAL**, and fails the job (exit code 1) on any
finding that is not recorded in the accepted-risk baseline
(`.trivyignore`).  Baseline entries carry an expiry so the allowlist cannot
rot.  The push to `ghcr.io/rbnbrls/finance-sync` runs only on `main`, after
the scan passes.

## Testing

### Production security configuration

Production startup requires an explicit `SECRET_KEY`, `MASTER_ENCRYPTION_KEY`,
`REDIS_URL` and non-wildcard `CORS_ORIGINS`. Webhooks require HTTPS and reject
private or loopback destinations. Set `TRUSTED_PROXY_IPS` only to the actual
reverse-proxy IPs/CIDRs; untrusted `X-Forwarded-For` headers are ignored.

There are three test suites, split by the `integration` and `e2e` pytest
markers:

### Unit tests (default, fast)

The default suite runs on **aiosqlite** (in-memory SQLite) and mocked
dependencies — no Docker, PostgreSQL or Redis required:

```bash
uv run pytest                        # or: make test
```

It covers everything under `tests/` except `tests/integration/` and
`tests/e2e/` (the `-m "not integration and not e2e"` deselect is applied
by the Makefile and CI).

### Integration tests (real PostgreSQL + Redis)

`tests/integration/` runs the same application code against **real,
ephemeral** PostgreSQL and Redis instead of SQLite mocks.  It covers:

* **Repositories / UnitOfWork** — real UUID PKs, JSONB round-trips,
  FK and unique-constraint behaviour (`test_repository_pg.py`)
* **Transactional outbox** — JSONB payloads, unique idempotency keys,
  OutboxPublisher poll/dispatch (`test_outbox_pg.py`)
* **Sync orchestrator** — full pipeline persistence, idempotent re-runs,
  rollback on failure (`test_sync_orchestrator_pg.py`)
* **Redis** — distributed locks (SET NX EX + Lua release), rate-limit
  counters, TTL cache semantics, and shared webhook delivery throttling
  (`test_redis_integration.py`)
* **Migrations** — `alembic upgrade head` on a fresh database, schema
  assertions, `downgrade base` round-trip (`test_migrations.py`)

Run it with Docker (ephemeral PG+Redis via compose):

```bash
make test-integration
```

or manually against any PG/Redis (e.g. CI service containers):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test \
TEST_REDIS_URL=redis://localhost:6379/15 \
uv run pytest -m integration -v
```

If `TEST_DATABASE_URL` / `TEST_REDIS_URL` are unset the integration tests
are skipped with a pointer to this section, so plain `pytest` stays green
on machines without Docker.

### Release 2 resource safeguards

Release 2 adds three production safeguards that are easy to miss during
local development:

* portfolio/security reads fetch the latest prices in one set-based query;
  migration `0037` adds the `(security_id, interval, timestamp DESC)` index
  supporting that access pattern;
* DeGiro imports enforce both a per-file limit and a combined upload-batch
  limit (`DEGIRO_IMPORT_MAX_BATCH_BYTES`, default 100 MiB) while streaming;
* webhook delivery uses Redis-backed per-webhook minute buckets when Redis is
  configured, so throttling is shared across API and worker replicas.

The upload limit should be sized together with reverse-proxy request limits.
The Redis limiter is intentionally fail-open to the existing local limiter if
Redis is temporarily unavailable; delivery is still bounded per process.

Release 8 extends the sync-service extraction with typed account, transaction
and holdings stages plus an immutable per-run context. Stage writes now pass
through `sync/persistence.py`; the orchestrator retains ownership of the
single UnitOfWork and its transaction lifecycle.

### E2E tests (API → worker exactly-once)

`tests/e2e/` brings up the **full stack** — the FastAPI app, the
background worker (`process_outbox_job`), real PostgreSQL and real Redis
— and drives a sync **through the HTTP API**, then runs the worker's
outbox consumer, exactly like a production deployment:

1. `POST /api/v1/sync/{provider}` (Bearer auth) runs the sync pipeline:
   accounts + transactions are upserted and `account.created` /
   `transaction.created` events land in the transactional outbox —
   all atomically.
2. The worker (`process_outbox_job`) polls the outbox and delivers each
   message to subscribed webhooks (a local capture server records every
   POST).
3. At-least-once delivery is simulated two ways: re-driving the same sync
   through the API (a redelivered sync job), and re-processing outbox
   messages that were delivered but whose `pending → sent` ack never
   committed (a worker crash between side effect and commit).

The suite then asserts the **exactly-once observable outcome**:

* **Transactions/accounts** — re-syncs upsert by
  `(tenant, provider, external_id)`; the row count and the set of
  external ids never grow.
* **Outbox entries** — `created` events are emitted only for new
  entities, and every message carries a unique `idempotency_key`
  (DB-unique); a redelivered sync adds no rows.
* **Export runs / sync runs** — redeliveries create no export runs;
  each sync *attempt* records its own `sync_runs` row (attempts are
  expected), but no outcome is duplicated.
* **Webhook fan-out** — the transport itself is at-least-once by design:
  a redelivered message triggers one more POST carrying the same
  `event_id`, which consumers dedupe on.  What never happens is a *new*
  domain event: the domain tables stay exactly-once.

Run it with Docker (same ephemeral PG+Redis stack as integration):

```bash
make test-e2e
```

or manually against any PG/Redis:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/finance_sync_test \
TEST_REDIS_URL=redis://localhost:6379/15 \
uv run pytest -m e2e -v
```

Like the integration suite, the e2e tests skip when the env vars are
unset and are excluded from the default `pytest` run and the CI unit job.

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `COOLIFY_API_TOKEN` | Coolify Bearer token for triggering deployments via `https://dev.7rb.nl/api/v1/deploy` |

### Docker Images

Built images are published to GitHub Container Registry:
- `ghcr.io/rbnbrls/finance-sync:latest` — latest `main` build
- `ghcr.io/rbnbrls/finance-sync:<sha>` — per-commit tagged image
- `ghcr.io/rbnbrls/finance-sync:<version>` + `<sha>` — release images
  (tag-triggered), **cosign-signed** (keyless, GitHub OIDC)

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Architecture decisions](docs/adr/)
- [API specification](docs/API.md)
- [Market intelligence (source layer)](docs/market-intelligence.md)
- [Holding relevance (news & events for your holdings)](docs/holding-relevance.md)
- [Holding relevance notifications](docs/holding-relevance-notifications.md)
- [Sync scheduling (Planning on Sync Runs)](docs/sync-schedules.md)
- [Connector connections (multi-connection model)](docs/connections.md)
- [Data model](docs/DATABASE.md)
- [Database migrations](docs/MIGRATIONS.md)
- [Upgrade notes](docs/UPGRADE.md)
- [Releases & rollback](docs/RELEASING.md)
- [Implementation roadmap](docs/ROADMAP.md)
- [Wealthfolio multi-device access](docs/wealthfolio-multi-device-access.md)

## Health monitoring

`finance-sync-monitor` (src/finance_sync/monitoring/health_monitor.py) is a
standalone health monitor: it checks the app/worker health endpoints, polls
the Coolify API for the application status and restart count, samples
container CPU/memory via `docker stats`, and files GitHub issues on crashes
and resource-threshold alerts (with daily dedup markers).

It is fully decoupled from Hermes — all configuration comes from the
environment and it is scheduled by systemd, not by Hermes cron.

Install and schedule (see `deploy/systemd/` for the units):

```bash
uv tool install .                       # provides the finance-sync-monitor binary
sudo install -m 644 deploy/systemd/finance-sync-monitor.{service,timer} /etc/systemd/system/
sudo tee /etc/finance-sync/finance-sync-monitor.env >/dev/null <<'EOF'
COOLIFY_API_TOKEN=your-coolify-token
GITHUB_TOKEN=your-github-token
EOF
sudo chmod 600 /etc/finance-sync/finance-sync-monitor.env
sudo systemctl daemon-reload
sudo systemctl enable --now finance-sync-monitor.timer
```

Required environment (no `~/.hermes` fallbacks):

| Variable | Purpose |
|----------|---------|
| `COOLIFY_API_TOKEN` | Coolify Bearer token for the app status / restart-count check |
| `GITHUB_TOKEN` | GitHub token used to file issues on crashes / alerts |
| `STATE_FILE` | State JSON path (default `/var/lib/finance-sync/finance-sync-monitor-state.json`, dir auto-created) |

Optional overrides: `COOLIFY_API_URL` (default `http://192.168.3.110:8000/api/v1`),
`COOLIFY_APP_UUID` (default `obcopz3142hxzs1zlie78amh`),
`MONITOR_HEALTH_BASE_URL` (default `https://<app-uuid>.7rb.nl`).

## Project principles

- Providers are plugins; application services and REST resources never depend on provider SDK models.
- PostgreSQL is the durable system of record. Redis is disposable cache, coordination, and rate-limit state.
- Synchronization is idempotent, observable, retryable, and produces durable domain events.
- The first release is a deployable modular monolith; service extraction is an operational decision, not a premature boundary.
