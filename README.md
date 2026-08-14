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
- [Database migrations](docs/MIGRATIONS.md)
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
