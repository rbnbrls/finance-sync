# Current implementation coverage

This page is a lightweight snapshot of the codebase, not a second product
specification or a historical release ledger. Verify details against the
source and generated OpenAPI document before planning work.

| Area | Current implementation |
|---|---|
| API | FastAPI, 131 paths and 147 documented operations under `/api/v1`; OpenAPI is generated from `create_app()`. |
| Persistence | PostgreSQL via SQLAlchemy/Alembic; current migration head is `0041_add_connection_test_metadata`. |
| Coordination | Redis-backed locks, caching and rate limiting; PostgreSQL remains durable state. |
| Authentication | Tenant-scoped JWT login/refresh, admin-key login and scoped API keys. |
| Ingestion | Eight built-in connectors plus Python entry-point discovery for external connectors. |
| Sync reliability | Idempotent upserts, sync runs, per-connection cursors, transactional outbox and retryable jobs. |
| Scheduling | Tenant-scoped ingestion/export schedules dispatched by the worker's minute tick. |
| Destinations | Wealthfolio, Actual Budget, Jupyter, Firefly III, Ghostfolio, InvestBrain and Securo adapters; persisted destination configuration is used where supported. |
| Enrichment | Price/FX/security enrichment, OpenBB integration and freshness metadata. |
| Intelligence | SEC EDGAR, SEC press releases and optional OpenBB source adapters, with review queue and holding-relevance feeds. |
| Operations | API/worker health endpoints, structured logs, optional GlitchTip/Sentry, systemd health monitor and release/migration runbooks. |
| Quality gates | Ruff, Pyright, pytest, coverage threshold 73%, OpenAPI diff, migration checks and container security checks in CI. |

## Known boundaries

- `/openapi.json` is authoritative for the REST surface; this page is only a
  map of capabilities.
- Integration and E2E tests require real PostgreSQL and Redis. Unit tests use
  isolated test dependencies and exclude those markers by default.
- Exporter availability is controlled by settings and destination state; a
  document describing an adapter does not mean that its remote service is
  configured in every deployment.
- Deployment-specific URLs, image tags, credentials and monitoring state do
  not belong in this repository-level coverage snapshot.
