# finance-sync

[![CI](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml)

|Self-hosted, API-first financial data platform. It imports provider data, normalizes it into a provider-independent ledger and portfolio model, enriches securities through OpenBB, and serves downstream applications such as Actual Budget and Wealthfolio.

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD (`.github/workflows/ci.yml`):

| Stage | Description |
|-------|-------------|
| **Lint** | Ruff check + format check |
| **Type check** | Pyright in strict mode |
| **Test** | Pytest unit suite (aiosqlite, no external services) with coverage threshold |
| **Migrations** | Alembic linear-chain check + `upgrade head` on an empty PostgreSQL |
| **Integration** | `pytest -m integration` against ephemeral PostgreSQL + Redis (repositories, outbox, sync orchestrator, Redis locks/rate-limits, migration upgrade/downgrade) |
| **E2E** | `pytest -m e2e` — full API → worker → outbox pipeline against ephemeral PostgreSQL + Redis, proving the exactly-once observable outcome under at-least-once delivery |
| **Security** | pip-audit vulnerability scan + CycloneDX SBOM |
| **Build & Push** | Docker image built with Buildx and pushed to `ghcr.io/rbnbrls/finance-sync` |
| **Deploy** | Triggers Coolify deployment on push to `main` |

## Testing

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
  counters, TTL cache semantics (`test_redis_integration.py`)
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

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Architecture decisions](docs/adr/)
- [API specification](docs/API.md)
- [Data model](docs/DATABASE.md)
- [Database migrations](docs/MIGRATIONS.md)
- [Upgrade notes](docs/UPGRADE.md)
- [Implementation roadmap](docs/ROADMAP.md)

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
