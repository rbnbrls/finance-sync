---
phase: 01
plan: 01
subsystem: wealthfolio-remediation
tags: [wealthfolio, remediation, health, scheduler, quotes, alembic]
requires: []
provides: [wealthfolio-health-polling, health-issue-backlog-bridge, safe-quote-repair]
affects: [worker, control-plane, data-health, observability]
tech-stack:
  added: []
  patterns: [bounded-polling, encrypted-target-secret-reuse, fail-closed-manual-review]
key-files:
  created:
    - src/finance_sync/models/wealthfolio_health_cursor.py
    - migrations/versions/0071_wealthfolio_health_cursors.py
    - src/finance_sync/services/wealthfolio_health_bridge.py
    - src/finance_sync/reconciliation/remediation/wealthfolio.py
    - docs/wealthfolio-remediation-bridge.md
  modified:
    - src/finance_sync/exporter/wealthfolio/client.py
    - src/finance_sync/worker/jobs.py
    - src/finance_sync/worker/scheduler.py
    - src/finance_sync/api/v1/control_plane.py
    - src/finance_sync/services/data_health.py
decisions:
  - Keep polling disabled by default and reuse encrypted ExportTarget credentials.
  - Resolve only canonical quote/history repairs after remote Wealthfolio verification.
  - Route unknown, purchase-price, and valuation findings to manual_review.
estimate: null
actuals:
  tokens: 13580
  tasks: 7
  commits: 7
plan_head_before: 2011c40facba856695f6dd5337bda77268324908
commits: 7
duration: 0 min
completed: 2026-09-12
status: complete
---

# Phase 1 Plan 1: Wealthfolio remediation bridge Summary

Authenticated Wealthfolio health polling now feeds a tenant-safe remediation backlog and repairs only canonical quote/history data with remote verification.

## Accomplishments

- Added `get_health_status()` with bounded retries and credential-safe error normalization.
- Added tenant/target health cursors and migration; raw responses and secrets are not persisted.
- Normalized affected Wealthfolio entities into stable remediation findings with idempotent deduplication and fail-closed manual review routing.
- Added Wealthfolio-aware quote and historical-price strategies that reuse canonical enrichment and `upsert_quote`.
- Added encrypted-target polling, scheduler registration, bounded limits, per-target failure isolation, and disabled-by-default configuration.
- Added tenant-scoped control-plane triggering, Data Health bridge aggregates, Prometheus metrics, rollout documentation, and focused contract tests.

## Verification

- Passed: `python3 -m compileall` over all changed Python modules and tests.
- Passed: `git diff --check`.
- Not run: pytest and Ruff; the checkout has no installed runtime/development dependencies (`structlog` and `ruff` are unavailable). No package installation was performed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected bridge deduplication key calculation**
- **Found during:** Task 7 close-out review
- **Issue:** `DetectedIssue` is immutable data and does not expose a `deduplication_key` attribute.
- **Fix:** Use the existing shared `deduplication_key()` function before reconciling disappeared findings.
- **Files modified:** `src/finance_sync/services/wealthfolio_health_bridge.py`
- **Commit:** `19ff61c`

## Deferred Issues

- Full pytest/Ruff/integration verification remains pending until the project development environment is installed.
- Canonical security identity enrichment from Wealthfolio ISIN/ticker payloads is intentionally limited to explicit identifiers; unresolved identity findings fail closed to manual review.

## Self-Check: PASSED

- Summary file exists at the expected phase path.
- All seven task commits are present in history: `a042f08`, `15cea55`, `682a19e`, `2c71bcd`, `b91f5f7`, `86f272c`, `19ff61c`.
- ROADMAP.md was not modified.
