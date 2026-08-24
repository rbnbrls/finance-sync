# Independence Audit — exporters & scheduled jobs vs. the Hermes runtime

**Audited commit:** `022362f` (main, "Fix: security audit vulns (#192)")
**Date:** 2026-08-14
**Scope:** every exporter, scheduled job, background worker, and cron-like
mechanism in the finance-sync repository, plus Hermes-side jobs that serve
finance-sync (the only place Hermes could leak in).

**Requirement (from issue #190 / roadmap delivery rules):** every exporter and
recurring job must have a standalone entrypoint (CLI command, script, binary,
or workflow) that does **not** depend on Hermes cron jobs or the Hermes runtime.

---

## Verdict summary

| Area | Jobs found | Standalone entrypoint | Hermes-dependent |
|---|---|---|---|
| Worker scheduled jobs (APScheduler) | 6 | ✅ `python -m finance_sync.worker` (own container) | ❌ none |
| Wealthfolio exporter | 2 triggers (CLI, REST) | ✅ CLI + API | ⚠️ daily automation only exists as an orphaned Hermes-side script |
| Actual Budget exporter | 0 triggers | ❌ **none** | ❌ n/a (untriggerable) |
| MCP server | 9 tools incl. `tool_run_sync` | ✅ `python -m finance_sync.mcp` / uvicorn | ❌ none (Hermes is a *client*, not a dependency) |
| CLI | reconcile / compare / wealthfolio | ✅ `python -m finance_sync` | ❌ none |
| GitHub Actions workflows | 4 | ✅ GitHub-native | ❌ none (`schedule:` unused) |
| Hermes cron jobs serving finance-sync | 2 | ❌ script-in-`~/.hermes` only | ✅ **yes — see F** |
| Passive infra (GlitchTip, worker health server, Redis) | — | ✅ | ❌ none |

**Non-compliant items (need remediation):**
1. **Actual Budget exporter has no trigger at all** — library code only.
2. **Wealthfolio automated delivery does not exist in-repo** — ARCHITECTURE.md
   promises an event-driven + 5-min sweep; the only daily automation is a
   Hermes-side script that is currently *not even scheduled*.
3. **`finance-sync-monitor` runs only via Hermes cron** and reads Hermes-side
   state (`~/.hermes/config.yaml`, `~/.hermes/.env`, `~/.hermes/*.json`).

---

## A. Worker scheduled jobs — ✅ COMPLIANT (8/8)

Entrypoint: `python -m finance_sync.worker`
(`src/finance_sync/worker/__main__.py` → `WorkerProcess`), deployed as its own
container in `docker-compose.yml` / `docker-compose.coolify.yml`
(`worker.entrypoint: ["python", "-m", "finance_sync.worker"]`).

- Scheduler: APScheduler `AsyncIOScheduler` with a `SQLAlchemyJobStore` in the
  same PostgreSQL DB (`src/finance_sync/worker/scheduler.py`) — job state
  survives worker restarts and is independent of any external scheduler.
- Runs **only** in the worker process. Verified: `lifespan.py` / `app.py` never
  start a scheduler or background task; the FastAPI app is job-free.
- All cadences are env-configurable (`WORKER_JOB_*`), defaults below.
- Post-audit additions: `sync_bunq_cards` (G-04, PR #200) and
  `export_wealthfolio` (R2, PR #217) — both registered in
  `WorkerScheduler._register_jobs` and env-gated.

| Job | Trigger (default) | Cadence | Hermes dep |
|---|---|---|---|
| `sync_bunq` | IntervalTrigger | every 15 min | none |
| `sync_bunq_cards` | IntervalTrigger | every 1 h | none |
| `sync_trading212` | IntervalTrigger | every 1 h | none |
| `enrich_prices` | CronTrigger (US market hours, Mon–Fri) | every 15 min, 09:30–16:00 EST | none |
| `nightly_reconciliation` | CronTrigger (`0 2 * * *` UTC) | daily | none |
| `process_outbox` | IntervalTrigger | every 30 s | none |
| `process_webhook_retries` | IntervalTrigger | every 30 s | none |
| `export_wealthfolio` | IntervalTrigger | every 5 min | none |

Implementation: `src/finance_sync/worker/jobs.py` (per-job async functions,
retry-with-backoff, monitoring via `JobMonitor`; health endpoint on
`WORKER_HEALTH_PORT` 9090).

## B. Exporters

### B1. Wealthfolio exporter — ⚠️ PARTIAL (entrypoints exist, automation gap)

Code: `src/finance_sync/exporter/wealthfolio/` (exporter, client, config,
models, transaction_mapper).

Existing standalone triggers:
- **CLI:** `python -m finance_sync wealthfolio export [--output-dir …]`
  (CSV) and `python -m finance_sync wealthfolio push [--server-url …]
  [--password …] [--dry-run]` (REST push to a Wealthfolio instance).
  Implemented in `src/finance_sync/cli.py` (`_cmd_wealthfolio_export` /
  `_cmd_wealthfolio_push`). Fully standalone — no Hermes involvement.
- **REST API:** `POST /api/v1/exporters/export` (authenticated) +
  `GET /exporters/config`, `GET /exporters/runs`, `GET /exporters/types`
  (`src/finance_sync/api/v1/exporters.py`). Needs the app running, but the app
  is standalone infra — no Hermes dependency.

Missing / non-compliant pieces:
- **No automated delivery in-repo.** `docs/ARCHITECTURE.md` §5 schedules
  `exporter delivery | event-driven + 5 min sweep` — no such job is registered
  in the worker (`scheduler.py` registers only the 6 jobs in §A). The API
  endpoint is push-on-demand only.
- **The only daily automation is a Hermes-side script that is not running:**
  `~/.hermes/scripts/wealthfolio-daily-sync.sh` (see §F2) is not referenced by
  any Hermes cron job today and its project path (`~/code/finance-sync`) does
  not exist. The "daily push" therefore does not happen at all right now.

> **Resolved (R2, PR #217):** the 5-minute `export_wealthfolio` delivery
> sweep is now registered in `worker/scheduler.py` (see §A, 8/8) and the
> Hermes-side script was deleted (R4). ARCHITECTURE.md §5 no longer
> promises "event-driven" delivery — it documents push-on-demand (REST
> API / CLI) plus the sweep (R6, PR #219).

### B2. Actual Budget exporter — ❌ NON-COMPLIANT (no trigger)

Code: `src/finance_sync/exporter/actual_budget/` (exporter, client, config,
models, transaction_mapper) + `ActualBudgetAccountMapping` model/repository +
full settings block (`ACTUAL_BUDGET_*` in `settings.py`).

- **Zero triggers.** Verified by search: `ActualBudgetExporter` is only defined
  and re-exported (`exporter/exporter.py`, `exporter/__init__.py`); there is
  no CLI subcommand, no API endpoint, no worker job, and no MCP tool that
  instantiates it. The only usage is a docstring example.
- The exporter cannot run at all — neither standalone nor via Hermes. It is
  dead code from an operations standpoint, despite being fully implemented.

### B3. Export-run persistence — ⚠️ note (G-01 dependency)

`ExportRun` / `ExportDelivery` / `ActualBudgetAccountMapping` tables exist only
as ORM models and are created via `Base.metadata.create_all` at app startup
(`lifespan.py`); **no Alembic migration covers them** (migrations only reach
the `0004_*` set for other tables). Already tracked as gap G-01 in
`docs/roadmap-coverage.md`. Relevant here because export runs recorded by
API/CLI-triggered exports would be lost on a migration-only rebuild of the DB.

## C. MCP server — ✅ COMPLIANT

Entrypoint: `python -m finance_sync.mcp` (SSE server, port 8100),
`uvicorn finance_sync.mcp.server:app`, or `mcp run src/finance_sync/mcp/server.py`
(`src/finance_sync/mcp/__main__.py`).

9 tools, including `tool_run_sync` (on-demand connector sync) and read tools
(summary, daily briefing, subscriptions, performance, allocation, cashflow,
sync-runs). `docs/MCP.md` documents Hermes Agent as one possible MCP *client* —
that is consumer-side usage, not a runtime dependency of any job. The MCP
server is standalone and Hermes-free.

## D. CLI — ✅ COMPLIANT

Entrypoint: `python -m finance_sync` (`src/finance_sync/__main__.py` →
`cli.py::main`). Subcommands: `reconcile`, `compare`, `wealthfolio export|push`.
All standalone; exit codes 0/1/2 documented. No `[project.scripts]` console
script is declared in `pyproject.toml` (only the `python -m` form) — worth
adding for ergonomics, but not a compliance issue.

## E. GitHub Actions workflows — ✅ COMPLIANT

- `ci.yml` — on push / pull_request (lint, type check, test, security, build & push).
- `deploy.yml` — on push to main + `workflow_dispatch`; triggers Coolify deploy webhook.
- `publish-sdk.yml` — on tag push.
- `ci-failure.yml` — `workflow_run` on CI failure; files GitHub issues.

**No `schedule:` (cron) trigger exists in any workflow.** All workflows run on
GitHub's infrastructure and are fully independent of Hermes.

## F. Hermes-side jobs serving finance-sync — ❌ NON-COMPLIANT (the dependency surface)

These live in `~/.hermes/scripts/` and are the only finance-sync work that
depends on the Hermes runtime. Neither is part of the repo.

### F1. `finance-sync-monitor` (Hermes cron `eac14957e1a0`, every 15 min)

Script: `~/.hermes/scripts/finance-sync-monitor.py` (630 lines). Checks
app/worker health endpoints, polls Coolify for restart count, samples
container CPU/mem via `docker stats`, and files GitHub issues on crashes and
resource-threshold alerts (with daily dedup markers).

Hermes dependencies (each violates the independence requirement):
1. **Trigger:** scheduled **only** by Hermes cron (`*/15 * * * *`, `no_agent`,
   deliver=local). No systemd timer, no crontab entry, no in-repo workflow.
2. **Hermes-side state (config):** falls back to reading `COOLIFY_API_TOKEN`
   from `~/.hermes/config.yaml` (`mcp_servers.coolify.headers.Authorization`,
   lines 28–40) when the env var is unset.
3. **Hermes-side state (secrets):** falls back to reading `GITHUB_TOKEN` from
   `~/.hermes/.env` (lines 186–199).
4. **Hermes-side state (data):** state file `~/.hermes/finance-sync-monitor-state.json`.
5. **Not in the repo:** the script and its tests (`test_finance_sync_monitor.py`)
   live under `~/.hermes/scripts/`, so the repo has no record of its existence.

Additional defect found during the audit: `check_coolify_app()` builds its
auth header with `f"Authorization: Bearer {GITHUB_TOKEN}"` (line 95), but no
module-level `GITHUB_TOKEN` exists — the script's own `TOKEN` (Coolify token,
read from env / `~/.hermes/config.yaml` at lines 27–40) is never used for this
call. The f-string therefore raises `NameError` on every run, the broad
`except` swallows it, and `check_coolify_app()` always returns
`status="error: name 'GITHUB_TOKEN' is not defined"`, `restart_count=-1`.
Restart-count-based crash detection therefore never fires; only the direct
health-endpoint check works. The correct fix is to pass `TOKEN`
(`COOLIFY_API_TOKEN`) in that header.

### F2. `wealthfolio-daily-sync.sh` (designed for Hermes cron 06:00 — currently UNSCHEDULED)

Script: `~/.hermes/scripts/wealthfolio-daily-sync.sh`. Header says "pushed to
Hermes cron via cronjob tool … invoked by the Hermes cron scheduler at 06:00
daily". Steps: sync Trading212 via an inline `asyncio` snippet calling
`sync_trading212_job`, then `python -m finance_sync.cli wealthfolio push`.

Hermes dependencies:
1. **Trigger:** designed for Hermes cron; **no cron job currently references it**
   (verified against `cronjob list` and `~/.hermes/cron/jobs.json`). Orphaned.
2. **Environment:** requires a local checkout at `~/code/finance-sync`
   (**does not exist**), its `.venv`, and `WEALTHFOLIO_SERVER_URL` /
   `WEALTHFOLIO_PASSWORD` from an unspecified `.env`.
3. **Logs** go to `~/.hermes/logs/`.

The underlying commands it wraps (`python -m finance_sync.cli …`) are
standalone, but the *trigger and environment* are Hermes-side, and today the
job is simply not running anywhere.

### F3. Out of scope (context only)

`poll-hermes-prs.py` (Hermes cron, every 10 min) reviews open PRs across all
rbnbrls repos including finance-sync. This is generic Hermes infrastructure for
the PR pipeline, not a finance-sync job or exporter; it does not provide any
functionality the repo needs at runtime.

---

## Remediation plan (for task decomposition)

| # | Item | Effort | Concrete steps |
|---|---|---|---|
| R1 | Give Actual Budget exporter a standalone CLI | S | Add `actual-budget push/export` subcommand to `src/finance_sync/cli.py` mirroring the Wealthfolio one (`ActualBudgetExporter` is already complete); add a `[project.scripts]` console entry for `python -m finance_sync` ergonomics; CLI test with mocked AB server. |
| R2 | In-repo Wealthfolio delivery sweep job | S–M | Register an `export_wealthfolio` job in `worker/scheduler.py` (IntervalTrigger, default 5 min) per ARCHITECTURE.md §5, calling `WealthfolioExporter.push_to_wealthfolio` for configured tenants; gate on `WEALTHFOLIO_SERVER_URL`/`WEALTHFOLIO_PASSWORD` being set; add env `WORKER_JOB_EXPORT_ENABLED`; unit test. |
| R3 | Port `finance-sync-monitor` into the repo and decouple from Hermes | M | Move script to `src/finance_sync/monitoring/health_monitor.py` (or `scripts/`); read tokens **only** from env (`COOLIFY_API_TOKEN`, `GITHUB_TOKEN`) with documented `.env`; move state to a data dir (e.g. `/var/lib/finance-sync/` or `STATE_FILE` env); fix the Coolify auth header (use `TOKEN`/`COOLIFY_API_TOKEN` — line 95 currently interpolates the undefined `GITHUB_TOKEN` and always NameErrors); ship a systemd timer unit (or Coolify scheduled task) as the standalone schedule; add tests; remove the `~/.hermes` copy and the Hermes cron job. |
| R4 | Fold / replace `wealthfolio-daily-sync.sh` | S | Preferred: delete it once R2 exists (the worker sweep covers daily push). Alternative: move into repo as `scripts/wealthfolio-daily-sync.sh` + systemd timer + documented env; remove `~/.hermes` copy; remove any Hermes cron reference. |
| R5 | Alembic migrations for export tables | S | Part of G-01; add `export_runs` / `export_deliveries` / `actual_budget_account_mappings` to the migration chain so export runs are durable without `create_all`. |
| R6 | Align ARCHITECTURE.md §5 with reality | S | Either implement the promised jobs (exporter sweep → R2; weekly fundamentals; hourly bunq cards/scheduled payments → G-04) or mark them explicitly as not-yet-implemented to stop doc/impl drift. |

Legend: S = small (≤ half-day), M = medium (≤ 2 days). All fixes keep jobs
inside the repo with standalone triggers (CLI / worker / systemd / GitHub
Actions) — none reintroduce Hermes cron.

**Remediation status**

| # | Status | Notes |
|---|---|---|
| R1 | ✅ DONE | `actual-budget export/push` CLI + console entry, PR #201. |
| R2 | ✅ DONE | `export_wealthfolio` job in `worker/scheduler.py` (IntervalTrigger, default 5 min), env-gated on `WORKER_JOB_EXPORT_ENABLED` (default: enabled only when `WEALTHFOLIO_SERVER_URL` and `WEALTHFOLIO_PASSWORD` are set). Shipped in PR #217. |
| R3 | ✅ DONE | Ported to `src/finance_sync/monitoring/health_monitor.py`; env-only tokens; `STATE_FILE` env; Coolify auth header fixed to use `COOLIFY_API_TOKEN`; systemd units in `deploy/systemd/`; tests in `tests/test_health_monitor.py` (incl. Coolify-auth path); `~/.hermes` script + test removed and Hermes cron `eac14957e1a0` deleted. Shipped in PR #206. |
| R4 | ✅ DONE | R2 scope; daily Wealthfolio push covered by the in-repo worker sweep (PR #217). `~/.hermes/scripts/wealthfolio-daily-sync.sh` deleted in R4 follow-up (no Hermes cron job referenced it; verified against `cronjob list` and `~/.hermes/cron/jobs.json`). |

The `~/.hermes` copies referenced in §F are removed where noted above;
no finance-sync job runs via Hermes cron anymore.

---

## Acceptance criteria check

- **Every exporter/job in the repository is covered:** ✅ §A–§E inventory all 8
  worker jobs, both exporters, MCP tools, CLI commands, and 4 workflows.
- **All Hermes dependencies explicitly recorded with recommended fixes:** ✅
  §F identifies the 2 Hermes-side jobs (monitor, daily-sync script) and §R1–R6
  give concrete remediation steps for the 3 non-compliant items
  (Actual Budget trigger, Wealthfolio automation, finance-sync-monitor).
