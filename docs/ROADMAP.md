# Implementation roadmap and issue plan

## Delivery rules

Every milestone ships migrations, typed configuration, structured logs, health signals, tests, documentation, and an upgrade note. Do not begin live connector work until mock/recorded contract fixtures and secret handling exist. Feature flags protect unfinished providers/exporters.

## Milestones

| Phase | Outcome | Prioritized issues / acceptance criteria |
|---|---|---|
| 1. Foundation | Deployable, secure skeleton | P0: pyproject/lint/type/test tooling; P0: FastAPI app/settings; P0: Postgres/Redis/Docker/Coolify; P0: Alembic core schema; P0: JWT/API keys/RBAC; P0: health/metrics/logging; P1: CI. A fresh deployment migrates, authenticates, and exposes readiness. |
| 2. Ingestion | Reliable provider-neutral facts | P0: connector SDK/registry; P0: sync-run/cursor/outbox; P0: canonical accounts/transactions/portfolio schema; P0: bunq accounts/balances/transactions; P0: Trading212 portfolio/holdings/cash/orders/dividends; P1: scheduled payments/cards; P1: reconciliation. Re-running a sync produces no duplicate facts/events. |
| 3. Enrichment | Market-data-backed security projections | P0: security/listing resolver; P0: OpenBB gateway and cache policy; P0: latest/historical prices; P1: fundamentals/ETF metadata; P1: FX valuation. Cached data honors TTL and records provenance/freshness. |
| 4. Consumer API | Stable downstream contracts | P0: read REST endpoints/OpenAPI; P0: portfolio, allocation, cashflow/net-worth services; P0: Actual Budget exporter; P0: Wealthfolio exporter; P1: exporter contract suites. Consumer failure retries without source data loss. |
| 5. Automation/insights | Operational integrations | P0: AI summary endpoints; P0: Home Assistant pull integration; P0: Grafana dashboard/alerts; P1: performance analytics; P1: subscription detection. Every aggregate declares as-of/freshness/coverage. |
|| 6. Ecosystem | Extensible platform | ✅ P0: versioned plugin SDK and compatibility policy; P1: MCP server; P1: additional connectors; P1: tax lots/calculations. Third-party plugin can be installed/configured without core source edits. |

## GitHub labels and issue ordering

Use labels `area:api`, `area:connector`, `area:data`, `area:enrichment`, `area:exporter`, `area:ops`, `security`, `good-first-issue`, `blocked:provider`, plus `priority:P0/P1/P2`. Close milestones only after automated acceptance tests run against PostgreSQL/Redis and recorded provider fixtures. Provider secrets and real account data are never prerequisites for ordinary CI.

## Testing and CI/CD

- Unit tests: domain policies/mappers with no I/O.
- Integration tests: async SQLAlchemy repositories, migrations, Redis locks/rate limits, outbox, with ephemeral PostgreSQL/Redis containers.
- Contract tests: connectors and exporters against provider/consumer fixtures; live tests are opt-in and secret-gated.
- E2E tests: API-to-worker flow verifies exactly-once observable outcome after at-least-once delivery.
- Quality gates: Ruff format/lint, pyright (strict for `app/`), pytest + coverage threshold ratcheted to 85% minimum, dependency/SBOM/image scans, OpenAPI diff, migration upgrade test.
- CI runs pull-request checks; a protected release pipeline builds immutable image tags, scans/signs them, runs migration job, deploys a staging stack, then promotes through Coolify. Production rollback is application-image rollback plus backward-compatible migration strategy—not blind schema downgrade.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Provider APIs change, limit, or omit history | Connector versioning, fixtures, capability declaration, backoff, manual sync/reconciliation. |
| Ambiguous ticker identity | ISIN/FIGI-first resolver, listing model, confidence score and review queue. |
| Duplicate/mutating transactions | Provider identity + fingerprint uniqueness, status/revision handling, outbox idempotency. |
| Stale/incomplete valuation | Per-field freshness, price provenance, coverage/caveats in every aggregate. |
| Credential/financial-data exposure | Envelope encryption, scoped access, redaction, audit, backups, security scans. |
| Exporter API mismatch | Isolated adapters, integration contract tests, delivery cursor and dead-letter visibility. |
| Premature distributed complexity | Modular-monolith boundaries; extract only after measured operational need. |

## Next release

Release 3 is gepland als een onderhoudbaarheidsrelease. Het uitvoerbare plan
staat in [RELEASE-3-PLAN.md](RELEASE-3-PLAN.md) en behandelt de decompositie
van de read API en sync-orchestrator, gestandaardiseerde foutafhandeling,
typekwaliteit, query-budgettests en dependency/documentatie-hygiëne.

De volgende release is Release 4. Het plan staat in
[RELEASE-4-PLAN.md](RELEASE-4-PLAN.md) en sluit de volledige read-/sync-
decompositie, echte PostgreSQL/Redis-gates, query-benchmarks en CI-warning-
budgetten af.

De daaropvolgende Release 5 focust op de daadwerkelijke service-extractie en
de volledige verificatie van integration-, migration-, E2E-, performance- en
typegates. Zie [RELEASE-5-PLAN.md](RELEASE-5-PLAN.md).

Release 6 rondt deze extractie en verificatie af. Het plan staat in
[RELEASE-6-PLAN.md](RELEASE-6-PLAN.md) en behandelt de resterende read-/sync-
componenten, endpoint query-budgets, benchmarks en real-service gates.

Release 7 vervolgt met de portfolio-, securities- en analytics-extractie,
holdings/persistence-stages en de volledige query-, benchmark- en real-service
gates. Zie [RELEASE-7-PLAN.md](RELEASE-7-PLAN.md).

Release 8 sluit de modularisering af met de volledige read-componenten,
`sync/persistence.py`, query-/benchmarkgates en formele PostgreSQL/Redis/E2E-
verificatie. Zie [RELEASE-8-PLAN.md](RELEASE-8-PLAN.md).

Release 9 is de afsluitende modulariserings- en productieverificatierelease.
Deze release verplaatst de concrete persistence-logica, extraheert de
portfolio-, securities- en analytics-readservices, voegt reproduceerbare
query-/benchmarkgates toe en voert de PostgreSQL/Redis/E2E-, security- en
stagingchecks uit. Zie [RELEASE-9-PLAN.md](RELEASE-9-PLAN.md).

Release 10 vervolgt de nog niet afgeronde delen van Release 9 met concrete
read/write-decompositie, echte database-querybudgetten, PostgreSQL/Redis/E2E-
gates en release-readiness. Zie [RELEASE-10-PLAN.md](RELEASE-10-PLAN.md).

Release 11 voert de daadwerkelijke portfolio-, securities-, analytics- en
persistence-extractie uit, koppelt querybudgetten aan echte PostgreSQL-sessies
en sluit de Redis-, migration-, E2E-, security- en staginggates. Zie
[RELEASE-11-PLAN.md](RELEASE-11-PLAN.md).

Release 12 is de afsluitende cleanup- en release-validatierelease. Deze
verwijdert de resterende legacyblokken, voert query-/benchmarkevidence uit,
draait de PostgreSQL/Redis/E2E- en securitygates en sluit staging-, rollback-
en documentatiechecks af. Zie [RELEASE-12-PLAN.md](RELEASE-12-PLAN.md).
