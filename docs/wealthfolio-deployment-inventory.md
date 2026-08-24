# Wealthfolio sync — live deployment inventory

Date: 2026-08-16 · Repo: rbnbrls/finance-sync · Inventory only, no code changes.
No secrets in this document (credential values are referenced by location only).

This is a point-in-time snapshot of the live deployment state taken to plan the
"make the Wealthfolio coupling live" story (backlog story
`wealthfolio-koppeling-live-bunq-trading212.md`). Follow-up configuration work
based on this inventory is tracked separately.

## 1. Coolify apps (both running:healthy)

| App | UUID | FQDN | Deployed commit | Container |
|---|---|---|---|---|
| finance-sync (production) | obcopz3142hxzs1zlie78amh | https://obcopz3142hxzs1zlie78amh.7rb.nl | dffe9a1 (= main HEAD, "Add DEGIRO pension import workflow (#238)") | obcopz3142hxzs1zlie78amh-100345470405 (uvicorn API only) |
| finance-sync-staging | mdeal4aqq9ycnozn3mg83zix | https://mdeal4aqq9ycnozn3mg83zix.7rb.nl | c4c5855 ("fix(ops): ship wget in production image … (#234)") — 1 commit behind main | mdeal4aqq9ycnozn3mg83zix-063130662014 (uvicorn API only) |

Health: both `/health/live` return HTTP 200.

## 2. Worker / scheduler deployment state — KEY FINDING

- **No worker container exists for either app.** `docker ps -a` on the Coolify
  host (LXC 100) shows exactly one container per finance-sync app (the
  API/uvicorn service). The `worker` service defined in the repo's
  `docker-compose.coolify.yml` (`python -m finance_sync.worker`, health port
  9090) is **not deployed**.
- Coolify generated a single-service compose for each app (container label
  `com.docker.compose.service` = the app container itself).
- Consequence: **the APScheduler worker is not running anywhere**, so NO
  scheduler job is active — not `export_wealthfolio`, not `sync_bunq`, not
  `sync_trading212`, not any other job.

## 3. Env vars — present / missing (Coolify-managed, confirmed at container runtime)

Neither app has any of the Wealthfolio/worker/connector env keys. Runtime
container env (`docker inspect .Config.Env`) grep for
`WEALTHFOLIO|WORKER_JOB|EXPORTER|BUNQ|TRADING212|DEGIRO` → **0 matches** in
both containers.

| Var | Prod | Staging | Effective value (code default) |
|---|---|---|---|
| EXPORTER_WEALTHFOLIO_ENABLED | MISSING | MISSING | true (default) — exporter API/CLI surface enabled |
| WEALTHFOLIO_OUTPUT_DIR | MISSING | MISSING | /tmp/finance_sync_wealthfolio_exports (default) |
| WEALTHFOLIO_DEFAULT_CURRENCY | MISSING | MISSING | EUR (default) |
| WEALTHFOLIO_SERVER_URL | MISSING | MISSING | "" (empty) — no push target configured |
| WEALTHFOLIO_PASSWORD | MISSING | MISSING | "" (empty) |
| WORKER_JOB_EXPORT_ENABLED | MISSING | MISSING | None → derived **false** (needs SERVER_URL + PASSWORD both set) → `export_wealthfolio` job NOT registered |
| WORKER_JOB_BUNQ_SYNC_ENABLED | MISSING | MISSING | true (default) — would register `sync_bunq` if worker ran |
| WORKER_JOB_TRADING212_SYNC_ENABLED | MISSING | MISSING | true (default) — would register `sync_trading212` if worker ran |

Present on prod only (not Wealthfolio-related): SECRET_KEY,
APP_ENVIRONMENT/NAME/VERSION, DEBUG, POSTGRES_PASSWORD, MASTER_ENCRYPTION_KEY,
LOG_LEVEL, DATABASE_POOL_MIN/MAX_SIZE, ACCESS_TOKEN_EXPIRE_MINUTES,
REFRESH_TOKEN_EXPIRE_DAYS, JWT_ALGORITHM,
DATABASE_URL, CORS_ORIGINS, REDIS_URL, REDIS_PASSWORD, GITHUB_TOKEN. Staging
has only the core subset (DATABASE_URL, REDIS_URL, APP_ENVIRONMENT, APP_NAME,
SECRET_KEY, MASTER_ENCRYPTION_KEY, LOG_LEVEL, CORS_ORIGINS).

Also missing in both deployments: any BUNQ_*/TRADING212_*/DEGIRO_* credentials
— the connector jobs would fail even if a worker were deployed.

## 4. Scheduler job `export_wealthfolio` — NOT active

Two independent blockers:

1. No worker container/process deployed (see §2).
2. Even with a worker: `worker_job_export_enabled` resolves to **false**
   because `WEALTHFOLIO_SERVER_URL` and `WEALTHFOLIO_PASSWORD` are both unset
   (settings.py: derived default = enabled only when both push-target vars are
   set).

## 5. Wealthfolio instance connectivity & auth (192.168.3.50:8080, LXC 104)

- Reachable from Hermes host: HTTP 200 on `/` and `/api/v1/auth/status`.
- Reachable from inside the prod app container (network path from Coolify host
  works): HTTP 200.
- Auth required: `{"requiresPassword": true, "oidcEnabled": false}`; login with
  empty password → HTTP 401.
- Auth with the current password: `POST /api/v1/auth/login` with `WF_PASSWORD`
  from `/root/wealthfolio.creds` on LXC 104 → **HTTP 200, auth OK**. (Value
  not printed; exists only on the LXC, not in Coolify.)
- The finance-sync client (`src/finance_sync/exporter/wealthfolio/client.py`)
  uses exactly this flow: `GET /api/v1/auth/status`, `POST /api/v1/auth/login`
  with `{"password": ...}` (API_PREFIX `/api/v1`).

## 6. Configuration changes needed (input for the config follow-up)

1. **Deploy the worker service** — the biggest gap. Env vars alone will not
   start the scheduler. Options: (a) make Coolify deploy the `worker` service
   from `docker-compose.coolify.yml` (currently only the app service is
   deployed), or (b) create a second Coolify app running
   `python -m finance_sync.worker`. Without this, `export_wealthfolio` can
   never be active.
2. **Set on the production app** (Coolify envs API):
   - `WEALTHFOLIO_SERVER_URL=http://192.168.3.50:8080`
   - `WEALTHFOLIO_PASSWORD=<value of WF_PASSWORD from /root/wealthfolio.creds on LXC 104>`
     (do not commit; set via Coolify API)
   - `WORKER_JOB_EXPORT_ENABLED=true` (explicit; would otherwise become true
     implicitly once the two above are set)
   - Optional: `EXPORTER_WEALTHFOLIO_ENABLED=true` (already default),
     `WEALTHFOLIO_OUTPUT_DIR`, `WEALTHFOLIO_DEFAULT_CURRENCY=EUR` (defaults are
     fine).
3. **bunq/Trading212 jobs**: flags default to true but **no connector
   credentials exist** in the deployment — the jobs will fail at runtime until
   BUNQ_*/TRADING212_* credentials are added. Confirm intended scope (story
   expects bunq + Trading212 data in Wealthfolio).
4. After configuring: restart/redeploy, then verify the worker process
   registers `export_wealthfolio` (e.g. worker logs / job summary) and that an
   end-to-end export against 192.168.3.50:8080 succeeds.
5. Staging mirrors production for dev; optionally apply the same env vars
   there.
