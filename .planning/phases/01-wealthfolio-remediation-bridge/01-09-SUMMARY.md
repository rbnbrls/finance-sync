---
phase: 01-wealthfolio-remediation-bridge
plan: 09
subsystem: api
tags: [wealthfolio, health, retry, remediation, metrics, data-health]
requires:
  - phase: 01-wealthfolio-remediation-bridge
    provides: Target-safe Wealthfolio remediation bridge and polling worker
provides:
  - Durable complete/truncated/cursor state for Wealthfolio health snapshots
  - Reconciliation guards that preserve active work on incomplete or failed polls
  - Bounded transport retries and feature-flag-aware Data Health reporting
affects: [wealthfolio-health, remediation, data-health, observability]
actuals:
  tokens: 6170
  tasks: 2
  commits: 2
plan_head_before: 2c5592fbc09d08f97b214cce8663315715fc0cbe
tech-stack:
  added: []
  patterns: [durable snapshot completeness, bounded transport retry, separate enablement and degradation signals]
key-files:
  created: []
  modified:
    - migrations/versions/0071_wealthfolio_health_cursors.py
    - src/finance_sync/models/wealthfolio_health_cursor.py
    - src/finance_sync/services/wealthfolio_health_bridge.py
    - src/finance_sync/worker/jobs.py
    - src/finance_sync/exporter/wealthfolio/client.py
    - src/finance_sync/services/data_health.py
    - src/finance_sync/api/v1/control_plane.py
    - src/finance_sync/schemas/data_health.py
    - src/finance_sync/observability/metrics.py
    - tests/test_wealthfolio_health_bridge.py
    - tests/test_phase01_wealthfolio_bridge.py
    - tests/exporter/test_wealthfolio_client.py
key-decisions:
  - "Only complete successful snapshots may reconcile omitted active findings."
  - "Transport RequestError failures retry within the existing three-attempt exponential bound; auth/configuration failures remain fail-fast."
  - "Data Health enabled reflects Settings.wealthfolio_health_bridge_enabled independently from active targets and degraded snapshot state."
patterns-established:
  - "Persist poll completeness and bounded cursor metadata on the existing tenant/target cursor."
  - "Expose incomplete snapshots through a credential-free reason-labelled metric."
requirements-completed: []
coverage:
  - id: D1
    description: "Incomplete and failed Wealthfolio snapshots preserve active remediation items and durable cursor state."
    verification:
      - kind: unit
        ref: "tests/test_wealthfolio_health_bridge.py::test_incomplete_success_persists_cursor_and_does_not_reconcile"
        status: pass
      - kind: unit
        ref: "tests/test_wealthfolio_health_bridge.py::test_failed_poll_persists_error_without_marking_cursor_complete"
        status: pass
      - kind: other
        ref: "APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py -q -k 'cap or truncat or complete or failure or disappearance or health_poll or cursor or metric'"
        status: pass
    human_judgment: false
  - id: D2
    description: "Transient transport errors retry safely and Data Health reports bridge enablement separately from target/degraded state."
    verification:
      - kind: unit
        ref: "tests/test_phase01_wealthfolio_bridge.py::test_health_poll_retries_transient_request_error"
        status: pass
      - kind: unit
        ref: "tests/test_phase01_wealthfolio_bridge.py::test_data_health_enabled_flag_is_separate_from_active_target"
        status: pass
      - kind: other
        ref: "APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py tests/exporter/test_wealthfolio_client.py -q"
        status: pass
    human_judgment: false
---

# Phase 01 Plan 09: Truthful Wealthfolio health state summary

**Durable completeness-aware Wealthfolio polling with bounded transport retries and truthful Data Health enablement/degradation reporting.**

## Performance

- **Duration:** 12 min
- **Started:** 2026-09-12T16:43:39Z
- **Completed:** 2026-09-12T16:54:31Z
- **Tasks:** 2
- **Files modified:** 12

## Accomplishments

- Replaced lossy issue slicing with a `HealthPollResult` completeness contract, persisted `complete`, `truncated`, and bounded `cursor_state`, and emitted incomplete-snapshot metrics.
- Prevented disappearance reconciliation for capped, malformed, partial, or failed polls while preserving active remediation work.
- Added bounded `httpx.RequestError` retry behavior with sanitized fail-fast auth/configuration paths.
- Wired the bridge Settings flag through both control-plane Data Health routes and separated `enabled`, target availability, and degraded/incomplete signals.

## Task Commits

Each task was committed atomically:

1. **Task 1: Trace capped and failed polls through durable lifecycle guards** - `347ba22` (fix)
2. **Task 2: Complete transport retry and feature-flag operational reporting** - `577b66e` (fix)

## Files Created/Modified

- `src/finance_sync/services/wealthfolio_health_bridge.py` - Completeness contract and guarded reconciliation.
- `src/finance_sync/worker/jobs.py` - Durable poll metadata and incomplete metric emission.
- `src/finance_sync/models/wealthfolio_health_cursor.py` and `migrations/versions/0071_wealthfolio_health_cursors.py` - Cursor persistence fields.
- `src/finance_sync/exporter/wealthfolio/client.py` - Bounded transient transport retry.
- `src/finance_sync/services/data_health.py`, `src/finance_sync/api/v1/control_plane.py`, and `src/finance_sync/schemas/data_health.py` - Truthful operational projection.
- `tests/test_wealthfolio_health_bridge.py`, `tests/test_phase01_wealthfolio_bridge.py`, and `tests/exporter/test_wealthfolio_client.py` - Lifecycle, retry, migration-shape, metrics, and Data Health regressions.

## Decisions Made

- A snapshot can reconcile disappearance only when the provider response is complete and successful; a complete empty response remains valid for reconciliation.
- Generic transport errors use the existing bounded exponential retry budget, while authentication and malformed/configuration failures do not retry.
- Feature enablement is sourced from Settings, not inferred from target count; target availability and degraded snapshot state remain independently observable.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Wired the Settings flag into the `/data-health` endpoint**
- **Found during:** Task 2
- **Issue:** The control-plane overview route passed the bridge flag, but the canonical `/data-health` route did not, so enabled status could still be inferred from the default constructor value.
- **Fix:** Passed `settings.wealthfolio_health_bridge_enabled` into `DataHealthService` for the Data Health route.
- **Files modified:** `src/finance_sync/api/v1/control_plane.py`
- **Verification:** Full plan-level suite passed (68 tests).
- **Committed in:** `577b66e`

---

**Total deviations:** 1 auto-fixed (Rule 2)
**Impact on plan:** Required correctness fix at the Settings-to-Data Health trust boundary; no scope creep.

## Issues Encountered

- Ruff remains `SKIPPED/UNVERIFIED`: the modified files contain pre-existing repository lint violations (exception-message style, line length, import formatting, and nested context warnings). The requested pytest verification is independent and passed.

## User Setup Required

PostgreSQL integration verification requires `TEST_DATABASE_URL` and a PostgreSQL service. No application configuration changes are required.

## Verification

- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py -q -k 'cap or truncat or complete or failure or disappearance or health_poll or cursor or metric'` — 16 passed, 13 deselected.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_phase01_wealthfolio_bridge.py tests/exporter/test_wealthfolio_client.py -q -k 'health_poll or request_error or transport or enabled or bridge'` — 14 passed, 37 deselected.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_data_health.py -q` — 66 passed.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py tests/exporter/test_wealthfolio_client.py -q` — 68 passed.
- **SKIPPED/UNVERIFIED:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m integration tests/integration/test_phase01_legacy_remediation_backfill.py -q` — 1 skipped because `TEST_DATABASE_URL`/PostgreSQL is unavailable; defer database-backed migration proof to 01-11.

## Known Stubs

None in the plan-created or plan-modified production surfaces. Empty collections found by the generic scan are intentional accumulators/test fixtures, not UI or data-source placeholders.

## Next Phase Readiness

The Wealthfolio bridge now preserves safe lifecycle semantics across incomplete and failed polls, retries transient transport outages, and reports operational state truthfully. PostgreSQL-backed migration proof remains explicitly deferred to 01-11 until `TEST_DATABASE_URL` is available.

## Self-Check: PASSED

- Summary file exists at the expected path.
- Task commits `347ba22` and `577b66e` are present in git history.
- Focused and plan-level verification results are recorded above.
