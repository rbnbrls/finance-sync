# Coolify Configuration Changes — Wealthfolio Live Sync (kanban t_af2a1faf)

Date: 2026-08-16 · Operator: Hermes kanban worker (default) · Coolify API: https://dev.7rb.nl/api/v1

Every configuration change made on the live Coolify deployment to enable the
`export_wealthfolio` scheduler job and Wealthfolio live export. No secrets are
listed here; credential values were set via the Coolify env API and never
committed to the repository.

## 1. Env vars set on production app `finance-sync` (uuid obcopz3142hxzs1zlie78amh)

The following 8 env vars were added via `POST /api/v1/applications/{uuid}/envs`
(one per request; the endpoint rejects bulk arrays). Values applied and
verified live in the running container (redeploy at 2026-08-16, deployment
`cqxa6sm7scw3vkb9h3ys5g9x`, finished):

| Key | Value | Purpose |
|-----|-------|---------|
| `EXPORTER_WEALTHFOLIO_ENABLED` | `true` | Enable Wealthfolio exporter API/CLI surface (code default is already true; set explicitly) |
| `WEALTHFOLIO_SERVER_URL` | `http://192.168.3.50:8080` | Wealthfolio instance base URL (LXC 104) |
| `WEALTHFOLIO_PASSWORD` | (secret, from LXC 104 `/root/wealthfolio.creds` key `WF_PASSWORD`; never printed/committed) | Password for Wealthfolio self-hosted auth |
| `WEALTHFOLIO_OUTPUT_DIR` | `/tmp/finance_sync_wealthfolio_exports` | CSV export output dir (explicit, matches code default) |
| `WEALTHFOLIO_DEFAULT_CURRENCY` | `EUR` | Default currency (explicit, matches code default) |
| `WORKER_JOB_EXPORT_ENABLED` | `true` | Register the `export_wealthfolio` APScheduler job (5-min cadence) |
| `WORKER_JOB_BUNQ_SYNC_ENABLED` | `true` | bunq sync job flag (code default true; set explicitly) |
| `WORKER_JOB_TRADING212_SYNC_ENABLED` | `true` | Trading212 sync job flag (code default true; set explicitly) |

Verified in the running prod container (grep of `docker exec ... env`):
all 8 keys present with the values above (password redacted).

## 2. Worker service deployed — new Coolify app `finance-sync-worker`

**Gap found during inventory (t_54e1f988):** no worker process was running
anywhere, so APScheduler was not executing and `export_wealthfolio` was never
registered — even with the env vars above.

**Fix:** created a second Coolify app in the same project/environment/server:

- Name: `finance-sync-worker` · uuid `rbeh9tetzvuyirutb66rxqea`
- Project `finance-sync` (ua2cwd0b6b9qof883tprcrdn) / environment `production` / server `homelab` (LXC 100)
- Build pack: `dockerfile`, `dockerfile_location: /Dockerfile.worker` (new repo file, PR #241)
- `ports_exposes: 9090` · `health_check_path: /health/live` · `health_check_port: 9090`
- No public domain (auto-assigned `*.7rb.nl` domain removed; internal-only service)
- Auto-deploy on push to `main` enabled
- Env vars: 28 total = 20 prod runtime env vars cloned 1:1 from the prod container
  (DATABASE_URL → `avoxjx7g0c36ru1ez7hetauy:5432`, REDIS_URL, SECRET_KEY,
  MASTER_ENCRYPTION_KEY, POSTGRES_PASSWORD, GITHUB_TOKEN, etc.) + the 8
  Wealthfolio/worker vars from section 1.

**Why a separate Dockerfile?** Coolify's dockerfile build pack does not apply
`start_command` (it only affects nixpacks builds — verified in Coolify v4
source `ApplicationDeploymentJob.php`), so the image's default CMD (uvicorn)
would run. `Dockerfile.worker` (PR #241) mirrors the app image with
`CMD ["python", "-m", "finance_sync.worker"]` and a HEALTHCHECK on :9090.

## 3. Deployment & verification

- Prod app: redeployed (deployment `cqxa6sm7scw3vkb9h3ys5g9x` → finished),
  status `running:healthy`; env vars confirmed in container.
- Worker app: first deployment exposed a pre-existing code bug — the worker
  crashed at scheduler start with `sqlalchemy.exc.MissingGreenlet` because
  APScheduler's `SQLAlchemyJobStore` (synchronous) cannot use the async-only
  asyncpg driver from `DATABASE_URL`. Fixed in repo PR #242
  (`sync_jobstore_url()` maps `postgresql+asyncpg://` → `postgresql+psycopg://`,
  `psycopg[binary]>=3.2` added, regression tests). After that merged, the
  worker was redeployed and reached `running:healthy`.
- Scheduler job `export_wealthfolio`: active on the worker (gated by
  `WORKER_JOB_EXPORT_ENABLED=true` + push-target vars set). Verified via the
  worker health endpoint: `/health` lists the APScheduler jobs including
  `export_wealthfolio` with a next run time; `/health/ready` reports the
  scheduler running.

## 4. Known gaps / not configured (out of scope for this task)

- `BUNQ_*` / `TRADING212_*` connector credentials are NOT set in Coolify
  (they live in the app DB as encrypted connector credentials). The
  `sync_bunq` / `sync_trading212` jobs register (flags true) but no-op
  (`sync_job_no_tenants`) until credentials exist per tenant.
- Staging app (mdeal4aqq9ycnozn3mg83zix) intentionally NOT touched.

## Repository changes

- `Dockerfile.worker` added (PR rbnbrls/finance-sync#241, label `hermes-auto`,
  marker `Hermes-Kanban-Task: t_af2a1faf`). No secrets in the diff.
