---
phase: 01-wealthfolio-remediation-bridge
plan: 08
subsystem: database
tags: [wealthfolio, remediation, deduplication, alembic, postgresql]
requires:
  - phase: 01-wealthfolio-remediation-bridge
    provides: Wealthfolio health normalization and remediation backlog
provides:
  - Target-scoped Wealthfolio remediation identity and exact connector routing
  - Fail-closed unsafe category classification
  - Migration 0072 legacy target identity backfill
affects: [remediation, worker, wealthfolio-health]
actuals:
  tokens: 4688
  tasks: 2
  commits: 2
plan_head_before: e4b30558b19177a4183ce3673ec8bc2fa0f3f8cc
commits: 2
tech-stack:
  added: []
  patterns: [tenant-scoped target identity, bounded legacy backfill, manual-review fail-closed routing]
key-files:
  created:
    - migrations/versions/0072_backfill_wealthfolio_remediation_target_identity.py
    - tests/integration/test_phase01_legacy_remediation_backfill.py
  modified:
    - src/finance_sync/services/wealthfolio_health_bridge.py
    - src/finance_sync/worker/jobs.py
    - tests/test_wealthfolio_health_bridge.py
key-decisions:
  - Use ExportTarget.id as the stable Wealthfolio identity in DetectedIssue.connection_id and scope.
  - Resolve remediation connectors only through exact tenant-scoped ExportTarget lookup by persisted connection_id.
  - Treat unsafe and unknown findings, plus ambiguous or unresolved legacy identity, as manual_review.
requirements-completed: []
coverage:
  - id: D1
    description: Wealthfolio findings remain independently deduplicated and route to their exact target.
    verification:
      - kind: unit
        ref: tests/test_wealthfolio_health_bridge.py::test_target_identity_is_part_of_backlog_identity
        status: pass
      - kind: other
        ref: APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py -q
        status: pass
    human_judgment: true
    rationale: PostgreSQL-backed connector routing was not executable without TEST_DATABASE_URL.
  - id: D2
    description: Unsafe findings and legacy identity gaps cannot select automatic remediation.
    verification:
      - kind: unit
        ref: tests/test_wealthfolio_health_bridge.py::test_unsafe_categories_precede_generic_price_and_are_manual_only
        status: pass
      - kind: integration
        ref: APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m integration tests/integration/test_phase01_legacy_remediation_backfill.py -q
        status: unknown
    human_judgment: true
    rationale: The PostgreSQL integration command was skipped because TEST_DATABASE_URL/PostgreSQL was unavailable.
duration: 14 min
completed: 2026-09-12
status: complete
---

# Phase 1 Plan 8: Target-safe Wealthfolio remediation summary

Wealthfolio remediation now carries stable ExportTarget identity through normalization, deduplication, persistence, and connector construction, while unsafe findings and uncertain legacy identity fail closed to manual review.

## Performance

- **Duration:** 14 min
- **Started:** 2026-09-12T16:27:00Z
- **Completed:** 2026-09-12T16:41:31Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments

- Added `connection_id` and `scope` to normalized Wealthfolio findings, preserving bounded `context.target_id` and isolating same-tenant targets in the deduplication key.
- Changed the remediation connector factory to perform an exact tenant-scoped `ExportTarget` lookup from persisted `connection_id`.
- Added unsafe-category precedence, explicit identifier types, migration 0072, and unit/PostgreSQL regression coverage for safe, unsafe, unique, ambiguous, and unresolved cases.

## Task Commits

1. **Task 1: Trace one Wealthfolio finding through target-scoped backlog registration** - `43915da` (fix)
2. **Task 2: Enforce unsafe category precedence and legacy fail-closed routing** - `8ef3dc2` (fix)

## Files Created/Modified

- `src/finance_sync/services/wealthfolio_health_bridge.py` - Target identity propagation, unsafe precedence, and identifier-type preservation.
- `src/finance_sync/worker/jobs.py` - Exact tenant-scoped Wealthfolio target lookup for remediation connectors.
- `migrations/versions/0072_backfill_wealthfolio_remediation_target_identity.py` - Bounded unique-target backfill and manual-review transitions.
- `tests/test_wealthfolio_health_bridge.py` - Target isolation, category precedence, and identifier regression tests.
- `tests/integration/test_phase01_legacy_remediation_backfill.py` - PostgreSQL migration coverage for unique, ambiguous, and unresolved legacy rows.

## Decisions Made

- ExportTarget database IDs are the stable target identity; mutable endpoint/account fields are never used for backfill or routing.
- A missing or non-unique legacy target match is retained as bounded context and routed to `manual_review`.
- Purchase/cost-basis, valuation, transaction/transfer, and unknown categories use no automatic strategy.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected the new transfer regression fixture**
- **Found during:** Task 2
- **Issue:** The fixture code `TRANSFER_WITHOUT_COST_BASIS` legitimately matched the higher-precedence cost-basis category, contradicting its expected transaction classification.
- **Fix:** Changed the fixture to `TRANSFER_MISMATCH`; both cases remain manual-review-only and the precedence contract is unambiguous.
- **Files modified:** `tests/test_wealthfolio_health_bridge.py`
- **Verification:** Combined focused suite passed with 22 tests.
- **Committed in:** `8ef3dc2`

---

**Total deviations:** 1 auto-fixed (Rule 1)
**Impact on plan:** Test-only correction; no scope expansion.

## Verification

- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py -q` — 4 passed.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_phase01_wealthfolio_bridge.py -q -k 'health_findings or unsafe or purchase or valuation or unknown'` — 1 passed, 10 deselected.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py -q` — 22 passed.
- **PASS:** Python compileall and `git diff --check`.
- **SKIPPED/UNVERIFIED:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m integration tests/integration/test_phase01_legacy_remediation_backfill.py -q` — 1 skipped because `TEST_DATABASE_URL`/PostgreSQL is unavailable. The migration regression was not reported as passed.

## Issues Encountered

- The first combined test run exposed a missing `pytest` import in the newly added parametrized test; it was fixed before the task commit.

## User Setup Required

PostgreSQL integration verification requires `TEST_DATABASE_URL` (and the repository integration service setup). No application configuration changes are required.

## Next Phase Readiness

The target identity and unsafe-routing gaps are implemented and unit-verified. PostgreSQL migration behavior remains unverified until a test database is available.

## Self-Check: PASSED

- Summary file exists at the expected path.
- Task commits `43915da` and `8ef3dc2` are present in git history.
- All planned created files exist.
