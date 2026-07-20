# finance-sync

Self-hosted, API-first financial data platform. It imports provider data, normalizes it into a provider-independent ledger and portfolio model, enriches securities through OpenBB, and serves downstream applications such as Actual Budget and Wealthfolio.

This repository is currently in the architecture phase. No connector or deployment implementation is included yet.

## Documentation

- [Architecture](/Users/ruben/Code/finance-sync/docs/ARCHITECTURE.md)
- [Architecture decisions](/Users/ruben/Code/finance-sync/docs/adr/)
- [API specification](/Users/ruben/Code/finance-sync/docs/API.md)
- [Data model](/Users/ruben/Code/finance-sync/docs/DATABASE.md)
- [Implementation roadmap](/Users/ruben/Code/finance-sync/docs/ROADMAP.md)

## Project principles

- Providers are plugins; application services and REST resources never depend on provider SDK models.
- PostgreSQL is the durable system of record. Redis is disposable cache, coordination, and rate-limit state.
- Synchronization is idempotent, observable, retryable, and produces durable domain events.
- The first release is a deployable modular monolith; service extraction is an operational decision, not a premature boundary.
