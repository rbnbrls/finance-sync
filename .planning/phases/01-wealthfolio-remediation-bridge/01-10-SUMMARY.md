---
phase: 01-wealthfolio-remediation-bridge
plan: 10
subsystem: remediation
tags: [wealthfolio, identity, canonical-security, historical-prices, intervals]
requires:
  - phase: 01-wealthfolio-remediation-bridge
    provides: Target-scoped Wealthfolio remediation bridge and truthful health lifecycle
provides:
  - Canonical security identity hydration for Wealthfolio quote/history findings
  - Fail-closed unresolved identity and runtime capability gating
  - Consistent half-open historical projection and verification windows
affects: [wealthfolio-health, remediation, enrichment, price-history]
actuals:
  tokens: 7023
  tasks: 2
  commits: 2
plan_head_before: 20667eb4ced77be85f659eff6b41cdb1c0fb3b18
tech-stack:
  added: []
  patterns: [canonical identity hydration, fail-closed capability gate, half-open time windows]
key-files:
  created:
    - tests/test_wealthfolio_remediation_contract.py
  modified:
    - src/finance_sync/services/wealthfolio_health_bridge.py
    - src/finance_sync/reconciliation/remediation/wealthfolio.py
    - src/finance_sync/reconciliation/remediation/price_history.py
    - src/finance_sync/worker/jobs.py
    - tests/test_wealthfolio_remediation_contract.py
key-decisions:
  - "Opaque Wealthfolio asset IDs are never treated as canonical finance-sync security IDs; automatic repair requires exactly one canonical match."
  - "Automatic Wealthfolio repair requires sanitized target/version capability evidence; missing or unsupported capability remains manual-review-only."
  - "Historical windows use [start,end) everywhere, with date-only ends normalized to the following midnight."
requirements-completed: []
coverage:
  - id: D1
    description: "Wealthfolio findings hydrate canonical security identity and preserve ISIN, ticker, and provider-symbol identifier types."
    verification:
      - kind: unit
        ref: "tests/test_wealthfolio_remediation_contract.py -k 'identity or asset or quote or identifier'"
        status: pass
    human_judgment: false
  - id: D2
    description: "Unresolved identity and unsupported target capability fail closed without selecting automatic repair."
    verification:
      - kind: unit
        ref: "tests/test_wealthfolio_remediation_contract.py::test_unresolved_or_ambiguous_identity_is_manual_only"
        status: pass
      - kind: manual_procedural
        ref: "A3 supported-target compatibility smoke against a deployed Wealthfolio target/version"
        status: unknown
    human_judgment: true
    rationale: "No supported Wealthfolio target/version was available in the execution environment."
  - id: D3
    description: "Historical projection, batching, and local/remote verification share half-open date semantics."
    verification:
      - kind: unit
        ref: "tests/test_wealthfolio_remediation_contract.py -k 'historical or interval or window or end_date'"
        status: pass
      - kind: other
        ref: "APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_remediation_contract.py tests/test_phase01_wealthfolio_bridge.py -q"
        status: pass
    human_judgment: false
---

# Phase 01 Plan 10: Canonical Wealthfolio repair identity and half-open history

**Canonical Wealthfolio asset identity with fail-closed capability gating and interval-consistent historical repair.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-09-12T16:56:00Z
- **Completed:** 2026-09-12T17:05:34Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments

- Added canonical `Security`/`SecurityListing` lookup for Wealthfolio findings, deriving a safe identifier and preserving explicit identifier types.
- Routed missing or ambiguous identity to `unsupported`/`manual_review`, and added the A3 sanitized capability gate so unsupported targets cannot enable repair.
- Normalized date-only historical ends to the next day and applied exclusive end bounds to enrichment, projection, batching, and remote verification.
- Added contract tests for asset-only identity, identifier types, unresolved identity, capability gating, invalid windows, and end-boundary behavior.

## Task Commits

Each task was committed atomically:

1. **Task 1: Trace one asset-only health finding through canonical lookup and remote quote projection** - `91fb96b` (fix)
2. **Task 2: Align historical projection and verification to half-open date windows** - `5dd25ca` (fix)

## Files Created/Modified

- `src/finance_sync/services/wealthfolio_health_bridge.py` - Canonical identity hydration and capability fail-closed handling.
- `src/finance_sync/reconciliation/remediation/wealthfolio.py` - Half-open remote projection and verification.
- `src/finance_sync/reconciliation/remediation/price_history.py` - Shared window normalization and safe provider execution.
- `src/finance_sync/worker/jobs.py` - Passes sanitized capability evidence into the bridge.
- `tests/test_wealthfolio_remediation_contract.py` - Identity, capability, invalid-window, and boundary regressions.

## Decisions Made

- Existing canonical security records are the only source of automatic repair identity; opaque Wealthfolio IDs are never guessed into local IDs.
- Target capability evidence is required before automatic repair; absent evidence leaves findings manual-only.
- The shared historical contract is `[start,end)` and date-only end values represent the full requested calendar day.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Validate historical windows before enrichment**
- **Found during:** Task 1 focused verification
- **Issue:** The provider-neutral historical strategy could call the enrichment gateway for a reversed window before rejecting it.
- **Fix:** Validate the normalized half-open window before any provider call and added a no-write regression test.
- **Files modified:** `src/finance_sync/reconciliation/remediation/price_history.py`, `tests/test_wealthfolio_remediation_contract.py`
- **Verification:** Invalid-window contract test and focused suites passed.
- **Committed in:** `91fb96b` (part of Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1)
**Impact on plan:** Necessary correctness fix; no architectural scope expansion.

## Issues Encountered

- The real A3 supported-target compatibility smoke was **SKIPPED/UNVERIFIED** because no supported Wealthfolio target/version was available locally. Automatic repair remains disabled and unverified unless runtime capability evidence is supplied.

## User Setup Required

None - no external service configuration was changed.

## Next Phase Readiness

Canonical identity and half-open historical semantics are unit-verified. Plan 01-11 can perform PostgreSQL-backed lifecycle/migration validation and a deployed-target A3 smoke when those environments are available.

## Verification

- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_remediation_contract.py -q -k 'identity or asset or quote or identifier'` — 3 passed, 5 deselected.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_remediation_contract.py -q -k 'historical or interval or window or end_date'` — 3 passed, 5 deselected.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_remediation_contract.py tests/test_phase01_wealthfolio_bridge.py -q` — 21 passed.
- **PASS:** `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_wealthfolio_health_bridge.py tests/test_phase01_wealthfolio_bridge.py tests/exporter/test_wealthfolio_client.py -q` — 68 passed.
- **PASS:** `PYTHONPATH=src python3 -m compileall -q src/finance_sync tests` and `git diff --check`.
- **SKIPPED/UNVERIFIED:** A3 supported-target compatibility smoke — no supported Wealthfolio target/version was available; the runtime gate fails closed.

## Known Stubs

None in the plan-created or plan-modified production surfaces.

## Self-Check: PASSED

- Summary file exists at the expected path.
- Task commits `91fb96b` and `5dd25ca` are present in git history.
- Measured plan commit count is 2 from `plan_head_before`.
- Focused and broader verification results are recorded above.

---
*Phase: 01-wealthfolio-remediation-bridge*
*Completed: 2026-09-12*
