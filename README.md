# finance-sync

[![CI](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml)

Self-hosted, API-first platform for synchronizing personal financial data.
finance-sync stores provider-independent accounts, transactions, holdings,
prices and enrichment data in PostgreSQL. Redis is used for coordination,
caching and rate limiting. Consumers such as Wealthfolio, Actual Budget,
Firefly III, Ghostfolio, InvestBrain, Securo and Jupyter are optional.

The current application version is `0.7.3` and requires Python 3.12 or newer.
Deployments are upgraded with **backward-compatible migrations**: the
application image can be rolled back while the database stays at its current
revision, so production recovery is an image rollback and never a blind
schema downgrade. Release 13 introduced the staged rollback and release
evidence checklist; release 14 kept the smoke evidence and OpenAPI contract
artifacts commit-bound. See [docs/RELEASING.md](docs/RELEASING.md).

## What is included

- FastAPI REST API under `/api/v1`, with interactive OpenAPI documentation at
  `/docs` and the generated schema at `/openapi.json`.
- Connector registry based on the `finance_sync.connectors` Python entry-point
  group. Built-ins are bunq, Trading 212, CSV import, manual expense,
  Plaid-like, YNAB, DEGIRO Pensioen and SaxoInvestor.
- Durable PostgreSQL model with tenant isolation, idempotent sync runs,
  transactional outbox, webhook delivery, sync cursors and export history.
- Optional market-data enrichment through OpenBB and the built-in SEC / SEC
  Press market-intelligence providers.
- A separate APScheduler worker for scheduled syncs, enrichment, outbox and
  webhook processing, DEGIRO watchfolders, reconciliation and export sweeps.
- Optional MCP server (`python -m finance_sync.mcp`) and CLI exporters and
  reconciliation commands.

## Quick start with Docker Compose

Create a `.env` with at least `POSTGRES_PASSWORD`, `SECRET_KEY` and
`ADMIN_KEY`, then start the stack:

```bash
docker compose up --build
```

Compose starts PostgreSQL and Redis, runs `alembic upgrade head`, and only
then starts the API and worker. The API is available at
`http://localhost:8000`; the worker health endpoint is at
`http://localhost:9090/health/live`.
The Compose debug switch is namespaced as `FINANCE_SYNC_DEBUG`, so an
unrelated host-level `DEBUG` value cannot prevent API startup.

Release 13 established the backward-compatible migration policy: deploy
expand/contract schema changes first, then use an immutable application-image
rollback (image rollback) when a release must be reverted. Production is never
downgraded blindly.

For local development:

```bash
uv sync --extra dev
uv run uvicorn finance_sync.main:app --reload
python -m finance_sync.worker
```

Never commit `SECRET_KEY`, `ADMIN_KEY`, connector credentials or
`MASTER_ENCRYPTION_KEY`. In production, `REDIS_URL`, `DATABASE_URL`,
`CORS_ORIGINS` and the encryption/authentication settings are validated at
startup.

## Development commands

```bash
make install       # install production and development dependencies
make lint          # Ruff
make format-check  # Ruff formatter check
make type          # Pyright
make test          # unit tests
make test-cov      # unit tests with coverage
make test-integration
make test-e2e
```

Integration and E2E tests use the PostgreSQL/Redis services from
`docker-compose.test.yml`. The default test command excludes both markers.
The coverage threshold is 73% (`pyproject.toml`).

Useful CLI groups are:

```bash
finance-sync reconcile --help
finance-sync compare --help
finance-sync wealthfolio --help
finance-sync actual-budget --help
finance-sync securo --help
finance-sync ghostfolio --help
finance-sync investbrain --help
```

## API and operational facts

The API currently exposes 131 paths and 147 documented operations. The
canonical contract is generated from the application; do not maintain a
second hand-written endpoint inventory. Use `docs/API.md` for the resource
map and examples, and `/openapi.json` for the complete contract.

Schema changes are owned by Alembic. The current linear migration head is
`0041_add_connection_test_metadata`; deployments run migrations before the
API and worker. See [docs/MIGRATIONS.md](docs/MIGRATIONS.md) and
[docs/UPGRADE.md](docs/UPGRADE.md).

## Documentation

Start at [docs/README.md](docs/README.md). The most useful references are:

- [Architecture](docs/ARCHITECTURE.md)
- [API guide](docs/API.md)
- [Connectors](docs/connectors-overview.md) and
  [connector development](docs/connector-development.md)
- [Destinations and exporters](docs/destinations.md)
- [Market intelligence](docs/market-intelligence.md) and
  [holding relevance](docs/holding-relevance.md)
- [MCP server](docs/mcp-server.md)
- [Migrations and upgrades](docs/MIGRATIONS.md) and
  [release operations](docs/RELEASING.md)

The remaining documents are focused runbooks or integration notes. Historical
release plans and superseded design notes are not treated as current product
documentation.

## License

MIT. See the project metadata in `pyproject.toml`.
