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
| **Security** | pip-audit vulnerability scan + CycloneDX SBOM |
| **Build & Push** | Docker image built with Buildx and pushed to `ghcr.io/rbnbrls/finance-sync` |
| **Deploy** | Triggers Coolify deployment on push to `main` |

## Testing

There are two test suites, split by the `integration` pytest marker:

### Unit tests (default, fast)

The default suite runs on **aiosqlite** (in-memory SQLite) and mocked
dependencies — no Docker, PostgreSQL or Redis required:

```bash
uv run pytest                        # or: make test
```

It covers everything under `tests/` except `tests/integration/` (the
`-m "not integration"` deselect is applied by the Makefile and CI).

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
