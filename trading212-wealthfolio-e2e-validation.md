# Trading212 → Wealthfolio end-to-end validation

Date: 2026-08-29
Repository: `rbnbrls/finance-sync`
Validated commit: `f09a8a6` (origin/main)

## Scope and environment

The assigned Kanban worktree was misregistered to the Coolify webhook
repository. Validation was therefore run against the intended finance-sync
checkout. No live Trading212 API key or Wealthfolio write credential was
available for this run. The end-to-end path was exercised with the repository's
representative Trading212 HTTP sandbox (`httpx.MockTransport`) and a real local
PostgreSQL test database (`finance_sync_test`, PostgreSQL via
`/home/hermes/pgsockets`). Wealthfolio API behavior was verified through its
recorded live contract fixtures and exporter contract tests; no production
write was attempted.

## Results

PASS — full non-integration/non-e2e regression:

    TEST_DATABASE_URL='postgresql+asyncpg://hermes@/finance_sync_test?host=/home/hermes/pgsockets' \
    TEST_REDIS_URL='redis://localhost:6379/15' \
    uv run pytest -q -m 'not integration and not e2e'

    3624 passed, 8 skipped, 200 deselected, 157 warnings

PASS — Trading212 connector, PostgreSQL pipeline, and Wealthfolio contracts:

    194 passed, 1 skipped, 1 warning

The PostgreSQL pipeline assertions confirmed:

- one Trading212 account, all three fixture holdings, and all order/cash
  transactions are persisted;
- a missing API key creates a failed sync without resource rows;
- a transaction-history failure rolls back account, holding, and transaction
  writes as one unit;
- repeating the same sync preserves account/security/transaction cardinality
  while correctly creating a new time-versioned holding snapshot.

PASS — explicit edge-case selection: 17 passed, 101 deselected. Coverage
included pagination, pending and partially filled orders, dividend mapping,
empty CSV holdings/transactions, and currency-sensitive mapping.

PASS — lint and typing:

- `uv run ruff check ...`: all checks passed;
- `uv run pyright`: 0 errors (62 pre-existing warnings);
- `git diff --check`: clean.

## Wealthfolio verification

The Wealthfolio exporter contract suite passed, including buy/sell/dividend,
deposit/withdrawal/interest/fee, empty exports, currency handling, holdings
CSV generation, security resolution, and idempotent account lookup behavior.
Recorded live-instance fixtures cover authenticated account discovery,
activities search, holdings response shape, and authentication failure behavior.
The existing live evidence (`evidence-live-wealthfolio-export.md`) records a
successful first import (3/3 activities, 0 failures) and a second pass with
zero duplicates.

## Issue #505

Issue #505's original failure mode (Trading212 history requests using a page
size above the API limit) is covered by the corrected connector and the passing
PostgreSQL-backed pipeline. The representative ingestion → persistence →
Wealthfolio export contract is green.

## Limitations

A fresh live Trading212 sync and live Wealthfolio holdings materialization
could not be claimed because this run had no live credentials. The recorded
Wealthfolio instance also has a known position-materialization limitation
documented in the existing evidence: activities import successfully and are
idempotent, but the remote `holdings/list` endpoint does not materialize the
security position. This is an instance-side computation/sync concern rather
than an ingestion or import failure.
