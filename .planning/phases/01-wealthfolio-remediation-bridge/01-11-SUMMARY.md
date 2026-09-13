---
phase: 01-wealthfolio-remediation-bridge
plan: 11
subsystem: testing
tags: [wealthfolio, remediation, postgresql, redis, alembic, integration]
requires:
  - phase: 01-wealthfolio-remediation-bridge
    provides: [target-scoped Wealthfolio bridge, complete-snapshot guards, verified executor transitions]
provides:
  - persisted Wealthfolio poll-to-resolution lifecycle regression
  - explicit 0071/0072 cursor migration contract assertions
  - honest PostgreSQL/Redis integration skip reporting
affects: [wealthfolio-remediation, integration-validation, release-verification]
tech-stack:
  added: []
  patterns: [fresh-session persistence assertions, remote-response-only doubles, explicit integration skips]
key-files:
  created:
    - tests/integration/test_phase01_wealthfolio_lifecycle.py
  modified:
    - tests/integration/test_phase01_wealthfolio_migration.py
key-decisions:
  - "Use real PostgreSQL sessions and persisted rows for every lifecycle claim; only remote Wealthfolio responses are doubled."
  - "Treat absent TEST_DATABASE_URL and TEST_REDIS_URL as SKIPPED/UNVERIFIED and retain the manual service checkpoint."
patterns-established:
  - "Lifecycle tests reload backlog items in fresh sessions after each transition to prove durable state rather than in-memory behavior."
  - "Integration validation reports missing external dependencies through pytest skip reasons and never fabricates a passing result."
requirements-completed: []
coverage:
  - id: D1
    description: "Database-backed Wealthfolio lifecycle covers poll idempotency, target isolation, incomplete/failed poll guards, retry_wait, and remote-verified resolution."
    verification:
      - kind: integration
        ref: "tests/integration/test_phase01_wealthfolio_lifecycle.py::test_persisted_wealthfolio_poll_to_resolution_lifecycle"
        status: unknown
    human_judgment: true
    rationale: "Integration test was explicitly skipped because PostgreSQL and Redis service variables were not configured; rerun against the repository services."
  - id: D2
    description: "Migration 0071/0072 validation asserts cursor columns, tenant-target uniqueness, foreign keys, and idempotent upgrade behavior."
    verification:
      - kind: integration
        ref: "tests/integration/test_phase01_wealthfolio_migration.py"
        status: unknown
    human_judgment: true
    rationale: "Migration integration tests were explicitly skipped because PostgreSQL and Redis service variables were not configured; retain manual service verification."
metrics:
  duration: 0
  completed: 2026-09-12
  status: complete
actuals:
  tokens: 3225.25
  tasks: 2
  commits: 3
plan_head_before: 1c6463e06468e92820bdab96df14bb16708fd1fd
commits: 2
duration: 0 min
completed: 2026-09-12
status: complete
---

# Phase 01 Plan 11: Persisted lifecycle and migration validation Summary

**PostgreSQL-backed Wealthfolio lifecycle and cursor migration regressions with explicit unavailable-service reporting**

## Performance

- **Duration:** 0 min
- **Started:** 2026-09-12T17:06:29Z
- **Completed:** 2026-09-12
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added a real-session lifecycle regression covering repeated-poll idempotency, two-target routing, incomplete and failed poll guards, claim persistence, failed remote verification to `retry_wait`, and subsequent verified resolution.
- Expanded migration assertions for `complete`, `truncated`, bounded `cursor_state`, tenant-target uniqueness, foreign keys, and idempotent full-chain upgrade through 0072.
- Ran both specified commands and recorded the exact pytest skip reason: `Integration tests need PostgreSQL + Redis. Run \`make test-integration\` (docker compose) or set TEST_DATABASE_URL / TEST_REDIS_URL — see README 'Integration tests'.`

## Task Commits

Each task was committed atomically:

1. **Task 1: Trace the persisted poll-to-resolution lifecycle with remote verification outcomes** - `3aa42c5` (test)
2. **Task 2: Execute or explicitly skip database migration validation** - `808b569` (test)

**Plan metadata:** `ad251ac` (docs: complete plan)

## Files Created/Modified

- `tests/integration/test_phase01_wealthfolio_lifecycle.py` - Persisted lifecycle regression using real PostgreSQL rows and Redis quota coordination.
- `tests/integration/test_phase01_wealthfolio_migration.py` - Cursor schema, constraints, and 0071/0072 upgrade assertions.

## Decisions Made

- Deterministic doubles model only remote Wealthfolio responses; lifecycle state is always queried from committed PostgreSQL rows in fresh sessions.
- Missing PostgreSQL/Redis configuration remains an explicit unverified integration result and a manual checkpoint per D-16.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The integration environment was unavailable: `TEST_DATABASE_URL` and `TEST_REDIS_URL` were both unset. The planned tests therefore skipped explicitly and were not treated as lifecycle or migration proof.

## User Setup Required

PostgreSQL and Redis integration services must be configured before final verification. Run:

`make test-integration`

or set `TEST_DATABASE_URL` and `TEST_REDIS_URL` as described by the repository integration harness, then rerun both plan commands.

## Manual Verification Checkpoint Retained

- **Gate:** blocking
- **Required:** rerun the lifecycle and migration commands with PostgreSQL and Redis available, then manually verify the control-plane/deployment checks remain enabled and target-scoped.
- **Current result:** `SKIPPED/UNVERIFIED`; no full integration verification claim is made.

## Self-Check: PASSED

- Summary file exists at the expected phase path.
- Task commits `3aa42c5` and `808b569` are present in history.
- Both specified pytest commands completed with explicit skip reasons.
- `compileall`, Ruff checks, formatting, and `git diff --check` passed.

---
*Phase: 01-wealthfolio-remediation-bridge*
*Completed: 2026-09-12*
