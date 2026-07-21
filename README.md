# finance-sync

[![CI](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/rbnbrls/finance-sync/actions/workflows/ci.yml)

|Self-hosted, API-first financial data platform. It imports provider data, normalizes it into a provider-independent ledger and portfolio model, enriches securities through OpenBB, and serves downstream applications such as Actual Budget and Wealthfolio.

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD (`.github/workflows/ci.yml`):

| Stage | Description |
|-------|-------------|
| **Lint** | Ruff check + format check |
| **Type check** | Pyright in strict mode |
| **Test** | Pytest with 85% coverage threshold |
| **Security** | pip-audit vulnerability scan + CycloneDX SBOM |
| **Build & Push** | Docker image built with Buildx and pushed to `ghcr.io/rbnbrls/finance-sync` |
| **Deploy** | Triggers Coolify deployment on push to `main` |

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
- [Implementation roadmap](docs/ROADMAP.md)

## Project principles

- Providers are plugins; application services and REST resources never depend on provider SDK models.
- PostgreSQL is the durable system of record. Redis is disposable cache, coordination, and rate-limit state.
- Synchronization is idempotent, observable, retryable, and produces durable domain events.
- The first release is a deployable modular monolith; service extraction is an operational decision, not a premature boundary.
