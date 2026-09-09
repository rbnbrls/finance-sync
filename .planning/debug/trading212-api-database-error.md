---
status: resolved
trigger: "zoek de root cause van de fout in de trading212 API en los deze op"
created: 2026-09-08
updated: 2026-09-08
---

# Trading212 API database error

## Symptoms

- Connection `20ac3d72-cdda-4a8e-bf10-a72736b9046d` reports `FOUT`.
- Last attempt: 2026-09-08 11:58:00.
- Last success: 2026-09-06 12:21:05.
- User-facing error: `Database error while syncing`.
- Accounts, holdings, and transactions are all stale/failed.

## Current Focus

- hypothesis: unknown; determine whether the failure is Trading212 API data, connector mapping, or persistence/database state.
- test: inspect production container logs, sync run details, connector implementation, and recent database exceptions.
- expecting: a reproducible exception and a focused regression test.
- next_action: gather initial evidence

## Evidence

- timestamp: 2026-09-08T00:00:00+02:00
  observation: session initialized from the user-reported connection failure.
- timestamp: 2026-09-08T10:00:00+02:00
  observation: worker log showed asyncpg DataError binding literal `None` to `$4::UUID` in the sync-run progress UPDATE, with `sync_run_id` logged as `None`.
- timestamp: 2026-09-08T10:09:37+02:00
  observation: after flushing the SyncRun on creation, the live Trading212 API sync completed: accounts=1, transactions=6, holdings=93, unresolved=0.

## Eliminated

## Resolution

- root_cause: SyncRun.id used a Python-side UUID default that was not materialized until SQLAlchemy flush. The first progress heartbeat ran before that flush and attempted to bind `None` as a PostgreSQL UUID.
- fix: flush the new SyncRun in `start_sync_run` before returning it, then use the materialized UUID for progress heartbeats.
- verification: PostgreSQL Trading212 integration suite 13 passed; live Trading212 sync completed at 2026-09-08 10:09:37 UTC and all three resources report healthy/non-stale.
- files_changed: src/finance_sync/sync/sync_run.py; tests/integration/test_trading212_sync_pipeline_pg.py
