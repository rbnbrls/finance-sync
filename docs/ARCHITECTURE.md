# Architecture

## Overview

finance-sync is a modular monolith with two long-running processes:

1. the FastAPI API process, which authenticates tenants and serves reads and
   writes;
2. the APScheduler worker, which runs connector syncs, tenant schedules,
   enrichment, reconciliation, outbox delivery and exports.

Both processes use the same application package and PostgreSQL database. They
do not share mutable in-memory state. Redis is disposable infrastructure for
locks, caches and rate limits; PostgreSQL is the durable system of record.

## Runtime boundaries

```text
provider APIs/files
        │
        ▼
connectors ──► sync pipeline ──► PostgreSQL canonical data
                                      │
                 ┌────────────────────┼───────────────────┐
                 ▼                    ▼                   ▼
             REST API            exporters            outbox/webhooks
                 │                    │                   │
                 ▼                    ▼                   ▼
          UI / integrations  Wealthfolio, etc.       external consumers
```

Connectors translate provider-specific data into canonical models. Services,
repositories and API schemas must not depend on provider SDK models. Exporters
translate canonical data into destination-specific formats or APIs.

## Package layout

| Package | Responsibility |
|---|---|
| `api/v1` | REST resources and request/response schemas |
| `connectors` | Built-in connectors and entry-point discovery |
| `sync` | Sync context, stages, persistence, cursors and outbox |
| `worker` | APScheduler, jobs, schedule dispatch and worker health |
| `models`, `db` | SQLAlchemy models, metadata, repositories and unit of work |
| `services/read` | Read-side queries, pagination and aggregate views |
| `exporter` | Destination adapters and export-run persistence |
| `enrichment`, `providers` | Prices, FX, security identity and OpenBB integration |
| `intel` | Market-intelligence providers, runs, review and credentials |
| `mcp` | MCP resources and tools over the same service layer |
| `observability`, `monitoring` | Health, structured logging, Sentry/GlitchTip and monitors |

## Data and consistency

The sync pipeline owns one UnitOfWork per run. Canonical upserts and domain
events are committed transactionally. Outbox delivery is at-least-once;
consumers must deduplicate by event identity. Connector retries and repeated
syncs are expected to be safe through tenant/provider/external identifiers and
sync cursors.

Credentials and other secret fields are encrypted when configured with
`MASTER_ENCRYPTION_KEY`. Tenant, connection and account scope is enforced at
the repository/service boundary and again by API dependencies where needed.

## Worker jobs

The worker registers jobs conditionally from settings. The current job families
are tenant schedule dispatch, connector sync, bunq card/scheduled-payment
sync, DEGIRO watchfolders, price enrichment, reconciliation, outbox and
webhook retries, Wealthfolio delivery, market-intelligence refresh and
holding-relevance feed building. A tenant's `sync_schedules` determine the
actual ingestion/export cadence; the scheduler itself uses a minute tick to
claim due work safely across restarts.

## Deployment

The production image runs as a non-root user and exposes port 8000. Compose
uses a one-shot `migrate` service before `app` and `worker`. The API health
endpoints are `/health`, `/health/live` and `/health/ready`; the worker
exposes the corresponding health endpoints on port 9090.

Alembic is the only schema owner. See [MIGRATIONS.md](MIGRATIONS.md) for the
current revision chain and [RELEASING.md](RELEASING.md) for promotion and
rollback rules.
