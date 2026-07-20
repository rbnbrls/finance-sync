# finance-sync

[![CI](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml)

Self-hosted, API-first financial data platform. It imports provider data, normalizes it into a provider-independent ledger and portfolio model, enriches securities through OpenBB, and serves downstream applications such as Actual Budget and Wealthfolio.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Architecture decisions](docs/adr/)
- [API specification](docs/API.md)
- [Data model](docs/DATABASE.md)
- [Implementation roadmap](docs/ROADMAP.md)

## Project principles

- Providers are plugins; application services and REST resources never depend on provider SDK models.
- PostgreSQL is the durable system of record. Redis is disposable cache, coordination, and rate-limit state.
- Synchronization is idempotent, observable, retryable, and produces durable domain events.
- The first release is a deployable modular monolith; service extraction is an operational decision, not a premature boundary.
