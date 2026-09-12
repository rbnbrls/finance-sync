---
phase: 01-wealthfolio-remediation-bridge
verified: 2026-09-12T16:05:00Z
status: gaps_found
score: 1/7 must-haves verified
covered_files:
  - .planning/phases/01-wealthfolio-remediation-bridge/01-PLAN.md
  - .planning/phases/01-wealthfolio-remediation-bridge/01-SUMMARY.md
  - .planning/phases/01-wealthfolio-remediation-bridge/01-REVIEW.md
  - .planning/phases/01-wealthfolio-remediation-bridge/01-VALIDATION.md
  - .planning/ROADMAP.md
  - .planning/STATE.md
  - .env.example
  - coolify.yaml
  - docs/wealthfolio-remediation-bridge.md
  - migrations/versions/0071_wealthfolio_health_cursors.py
  - src/finance_sync/api/v1/control_plane.py
  - src/finance_sync/config/settings.py
  - src/finance_sync/exporter/wealthfolio/__init__.py
  - src/finance_sync/exporter/wealthfolio/client.py
  - src/finance_sync/models/__init__.py
  - src/finance_sync/models/wealthfolio_health_cursor.py
  - src/finance_sync/observability/metrics.py
  - src/finance_sync/reconciliation/remediation/executor.py
  - src/finance_sync/reconciliation/remediation/wealthfolio.py
  - src/finance_sync/schemas/data_health.py
  - src/finance_sync/services/data_health.py
  - src/finance_sync/services/wealthfolio_health_bridge.py
  - src/finance_sync/worker/jobs.py
  - src/finance_sync/worker/scheduler.py
  - tests/exporter/test_wealthfolio_client.py
  - tests/test_phase01_wealthfolio_bridge.py
  - tests/test_wealthfolio_health_bridge.py
  - tests/integration/test_phase01_wealthfolio_migration.py
covered_digest: "v1:sha256:db03662b49229346843d42ae979ed6f543a8b208cefac5b367c2e2a4a09486b8"
behavior_unverified: 2
overrides_applied: 0
gaps:
  - truth: "Unsafe purchase-price, valuation, and unknown findings become manual_review without mutation"
    status: failed
    reason: "MISSING_PURCHASE_PRICE is classified as wealthfolio_quote_sync_failure because the generic price branch runs before the purchase/cost-basis branch."
    artifacts:
      - path: "src/finance_sync/services/wealthfolio_health_bridge.py:34-39"
        issue: "Purchase-price findings are assigned the automatic quote strategy."
      - path: "tests/test_phase01_wealthfolio_bridge.py:121-149"
        issue: "The focused behavioral test fails on the expected manual-review classification."
    missing:
      - "Classify unsafe purchase/cost-basis categories before generic price matching and retain a regression test."
  - truth: "Repeated polling does not create duplicate remediation items and remains target-safe"
    status: failed
    reason: "The target identity is stored only in JSON context, while the shared deduplication key excludes context; two targets in one tenant with the same issue/entity collide."
    artifacts:
      - path: "src/finance_sync/services/wealthfolio_health_bridge.py:115-124"
        issue: "DetectedIssue leaves connection_id and scope unset."
      - path: "src/finance_sync/reconciliation/remediation/backlog.py:77-91"
        issue: "The deduplication key uses connection_id/scope but not target_id context."
    missing:
      - "Include a stable Wealthfolio target identity in the deduplication identity and add a two-target regression test."
  - truth: "Resolved issues are marked resolved only after a complete successful remote verification"
    status: failed
    reason: "The worker truncates the remote issue list before enqueue_success(), which treats the truncated list as a complete successful snapshot and resolves omitted active findings."
    artifacts:
      - path: "src/finance_sync/worker/jobs.py:120-123"
        issue: "The configured issue limit is applied directly to the health payload."
      - path: "src/finance_sync/services/wealthfolio_health_bridge.py:169-188"
        issue: "Disappearance reconciliation resolves active issues absent from the supplied list without a completeness guard."
    missing:
      - "Skip disappearance reconciliation for capped responses or fetch a complete/paginated issue set first."
  - truth: "Safe quote and historical-price issues are repaired from canonical data and sent back to Wealthfolio"
    status: failed
    reason: "The strategy and upsert path are wired, but normalization does not resolve a common Wealthfolio asset id to a canonical security_id; such findings fail supports() and route to manual_review. ISIN/ticker context also lacks identifier_type and can be queried as the wrong identifier."
    artifacts:
      - path: "src/finance_sync/services/wealthfolio_health_bridge.py:81-109"
        issue: "The bridge copies remote identifiers but does not perform canonical security resolution."
      - path: "src/finance_sync/reconciliation/remediation/wealthfolio.py:21-35,78-85"
        issue: "Automatic strategies require security_id and do not consume the copied identifier safely."
    missing:
      - "Resolve remote assets using the canonical security lookup, preserve identifier_type, and add an end-to-end quote/history repair test."
  - truth: "Auth failures, rate limits, retries, unknown categories, and tenant-safe observability are all covered"
    status: failed
    reason: "HTTP status retries and timeout retries exist, but non-timeout httpx.RequestError is raised immediately; Data Health also reports enabled from target_count rather than the feature flag."
    artifacts:
      - path: "src/finance_sync/exporter/wealthfolio/client.py:223-240"
        issue: "DNS, connection-reset, and other transport failures are not retried."
      - path: "src/finance_sync/services/data_health.py:252-259"
        issue: "enabled is target_count > 0 and can contradict the disabled configuration."
    missing:
      - "Retry the bounded transient transport category and report the actual bridge flag separately from target availability."
deferred: []
behavior_unverified_items:
  - truth: "Wealthfolio quote issues appear as deduplicated rows in data_quality_remediation_items"
    test: "Run a successful health poll against a database-backed target and query the resulting backlog rows, then repeat the same poll."
    expected: "The quote/history findings are present once per target/entity and the second poll only updates last_seen_at/context."
    why_human: "The focused tests cover normalization but no active test exercises the enqueue_success database transition."
  - truth: "Resolved issues become resolved only after remote Wealthfolio health confirms the repair"
    test: "Execute one repair with remote verification returning false, then one with the finance-sync-owned quote present."
    expected: "The first attempt remains retry_wait (or manual_review after the configured limit), and only the confirmed attempt becomes resolved."
    why_human: "Executor wiring calls verify before transition, but no named test exercises the complete executor state transition."
decision_coverage:
  honored: 0
  total: 0
  not_honored: []
---

# Phase 1: Wealthfolio remediation bridge Verification Report

**Phase Goal:** The scheduled finance-sync worker polls authenticated Wealthfolio targets, deduplicates and queues Wealthfolio health issues, repairs safe quote and historical-price issues, sends repaired data back to Wealthfolio, and marks items resolved only after Wealthfolio health confirms the repair.
**Verified:** 2026-09-12T16:05:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|---|---|---|
| 1 | Repeated polling does not create duplicate remediation items | ✗ FAILED | Same-tenant targets collide because `target_id` is only context; a direct `uv run` check produced `same_key=True` for two target IDs. |
| 2 | Wealthfolio quote issues appear in `data_quality_remediation_items` | ⚠️ PRESENT_BEHAVIOR_UNVERIFIED | `wealthfolio_health_sync_job()` calls `enqueue_success()` and `BacklogRepository.register()`, but no active test exercises the database enqueue/idempotency transition. |
| 3 | Safe quote repairs update canonical data and Wealthfolio | ✗ FAILED | Strategies query `SecurityPrice` and call `upsert_quote`, but normalizer does not resolve common remote asset IDs to `security_id`; supported repair is not reliable for the advertised health payload shape. |
| 4 | Resolved issues become resolved only after remote verification | ⚠️ PRESENT_BEHAVIOR_UNVERIFIED | `RemediationExecutor` executes then calls Wealthfolio `verify()` before transitioning to `resolved`, but no named test proves the failed-verification `retry_wait` transition. The issue-cap path can nevertheless falsely resolve omitted findings. |
| 5 | Missing purchase prices and negative valuations are manual review, never fabricated | ✗ FAILED | The focused test fails: `MISSING_PURCHASE_PRICE` becomes `wealthfolio_quote_sync_failure`; this can select automatic quote repair. |
| 6 | Auth failures, rate limits, retries, unknown categories, and observability are tenant-safe | ✗ FAILED | Auth/rate-limit/malformed paths are classified and logs omit payloads, but non-timeout transport failures are not retried and Data Health can claim enabled while polling is disabled. |
| 7 | The bridge is disabled by default until deployment configuration is verified | ✓ VERIFIED | `Settings` defaults `WEALTHFOLIO_HEALTH_BRIDGE_ENABLED` to false; scheduler registration is gated by it; focused test passed; `.env.example` and `coolify.yaml` document disabled-first rollout. |

**Score:** 1/7 truths verified (2 present, behavior-unverified)

### Blocking Open Threats

The three blocking threats called out by the phase review remain reproducible and prevent progression:

1. Purchase-price findings can enter automatic quote repair (`CR-01`; also the one failing validation test).
2. Two Wealthfolio targets can share and overwrite one backlog item (`CR-02`; direct deduplication check reproduced this).
3. Capped health responses can falsely resolve omitted issues (`CR-03`; truncation occurs before disappearance reconciliation).

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/finance_sync/exporter/wealthfolio/client.py` | Authenticated health polling and bounded error handling | ✓ VERIFIED | Substantive client, imported by the worker, and used for `/api/v1/health/status`; transport retry omission remains a gap. |
| `src/finance_sync/models/wealthfolio_health_cursor.py` | Tenant/target polling state | ✓ VERIFIED | Model is registered and backed by migration `0071`; PostgreSQL integration test was skipped because no test database is configured. |
| `migrations/versions/0071_wealthfolio_health_cursors.py` | Cursor table, uniqueness, indexes | ✓ VERIFIED | Migration defines tenant/target FKs, uniqueness, and operational indexes; application was compile-checked, not applied here. |
| `src/finance_sync/services/wealthfolio_health_bridge.py` | Normalize, bound, enqueue, reconcile health issues | ⚠️ HOLLOW | Wired to the worker/backlog, but target identity is absent from deduplication and capped snapshots are treated as complete. |
| `src/finance_sync/reconciliation/remediation/wealthfolio.py` | Canonical quote/history projection and remote verification | ⚠️ HOLLOW | Queries canonical prices and calls `upsert_quote`; identity resolution is incomplete and lifecycle behavior lacks an end-to-end test. |
| `src/finance_sync/reconciliation/remediation/executor.py` | Verify before resolution and retry failed verification | ✓ VERIFIED | Strategy execution is followed by `verify()` and status selection; transition behavior is untested. |
| `src/finance_sync/worker/jobs.py` / `src/finance_sync/worker/scheduler.py` | Scheduled polling and bounded remediation | ✓ VERIFIED | Poll job is registered behind the feature flag, isolates target exceptions, and registers remediation strategies. Issue-cap lifecycle bug remains. |
| `src/finance_sync/api/v1/control_plane.py` | Permissioned tenant-scoped manual trigger | ✓ VERIFIED | Endpoint requires `sync:write` and passes `auth.tenant_id`; authorization and external execution remain manual-only. |
| `src/finance_sync/services/data_health.py` / `src/finance_sync/observability/metrics.py` | Bridge status and credential-free metrics | ⚠️ PARTIAL | Cursor aggregates and metrics exist; enabled status is not sourced from configuration and no deployed observability check was possible. |
| `tests/test_phase01_wealthfolio_bridge.py` / `tests/test_wealthfolio_health_bridge.py` | Contract and behavioral tests | ⚠️ PARTIAL | Focused run: 10 passed, 1 failed. No cross-target, capped-snapshot, executor lifecycle, or end-to-end repair test. |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `wealthfolio_health_sync_job` | Wealthfolio `/api/v1/health/status` | `WealthfolioClient.get_health_status()` | ✓ WIRED | Authenticates and calls the endpoint for each active target. |
| `wealthfolio_health_sync_job` | `WealthfolioHealthBridge` | `enqueue_success()` / `record_failure()` | ✓ WIRED | Per-target loop continues after failures. |
| `WealthfolioHealthBridge` | remediation backlog | `BacklogRepository.register()` | ✓ WIRED | Real persistence path exists, but target identity is not part of the key. |
| remediation executor | Wealthfolio quote/history strategies | strategy registry and `connector_factory` | ✓ WIRED | Both strategies are registered and target credentials are tenant-checked. |
| strategy execution | remote resolution | `strategy.verify()` before `backlog.transition(... resolved)` | ✓ WIRED | Ordering is explicit in executor; transition behavior is not behaviorally proven. |
| feature flag | scheduler | `if settings.wealthfolio_health_bridge_enabled` | ✓ WIRED | Disabled default prevents scheduled bridge registration. |
| control-plane endpoint | worker job | permission dependency and `tenant_id=auth.tenant_id` | ✓ WIRED | Tenant-scoped trigger exists; human authorization smoke test remains. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|---|---|---|---|---|
| `wealthfolio_health_sync_job` | `payload` | Authenticated Wealthfolio HTTP response | Yes | ✓ FLOWING |
| `wealthfolio_health_bridge.py` | `findings` | Remote `issues` normalized into `DetectedIssue` | Yes | ⚠️ STATIC/PARTIAL | The list is real, but the worker caps it before reconciliation. |
| `BacklogRepository.register` | remediation item | PostgreSQL `data_quality_remediation_items` insert/upsert | Yes | ✓ FLOWING | DB-backed enqueue path is wired; active test is missing. |
| `WealthfolioQuoteStrategy` | latest `SecurityPrice` row | Canonical DB query | Yes when `security_id` exists | ⚠️ HOLLOW | Common remote asset-only findings do not obtain `security_id`. |
| `WealthfolioQuoteStrategy` | remote quote | `connector.upsert_quote()` | Yes | ✓ FLOWING | Target connector is created from the encrypted target secret. |
| executor | final status | `strategy.verify()` result | Yes | ⚠️ PRESENT, UNPROVEN | Code orders verification before resolution, but no lifecycle test executes it. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Phase bridge contract tests | `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_phase01_wealthfolio_bridge.py -q` | `10 passed, 1 failed`; `MISSING_PURCHASE_PRICE` expected manual review but got `wealthfolio_quote_sync_failure` | ✗ FAIL |
| Default disabled configuration | Included in focused command above | `test_bridge_is_disabled_by_default` passed | ✓ PASS |
| Cross-target dedup identity | `uv run python` direct `deduplication_key()` comparison | `same_key=True` for `target-a` and `target-b` | ✗ FAIL |
| Cursor migration contract | `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m integration tests/integration/test_phase01_wealthfolio_migration.py -q` | `1 skipped` because PostgreSQL/test database is unavailable | ? SKIP |
| Syntax/whitespace sanity | `PYTHONPATH=src python3 -m compileall -q src/finance_sync tests`; `git diff --check` | Both exit 0 | ✓ PASS |

### Probe Execution

No phase-declared or conventional `scripts/*/tests/probe-*.sh` probe was found.

### Requirements Coverage

No `.planning/REQUIREMENTS.md` exists and the plan declares no requirement IDs. Roadmap success criteria were therefore used as the contract.

### Decision Coverage

No phase `CONTEXT.md` exists. `check.decision-coverage-verify` returned `skipped: true`, `total: 0`; no decision coverage finding is applicable.

### Test Quality Audit

| Test File | Linked Requirement | Active | Skipped | Circular | Assertion Level | Verdict |
|---|---|---:|---:|---:|---|---|
| `tests/test_phase01_wealthfolio_bridge.py` | Phase success criteria | 11 | 0 | 0 | Value/behavioral for normalization and client errors | PARTIAL — one active test fails; no DB lifecycle proof |
| `tests/test_wealthfolio_health_bridge.py` | Deduplication/normalization | 3 | 0 | 0 | Value | PASS for isolated normalization only |
| `tests/integration/test_phase01_wealthfolio_migration.py` | Tenant/target cursor state | 1 | 1 environment skip | 0 | Value/schema | WARNING — not executed without PostgreSQL |
| `tests/exporter/test_wealthfolio_client.py` | Client contract | existing client suite | 0 observed | 0 observed | Value/status | Supporting coverage; not sufficient for bridge lifecycle |

**Disabled tests on requirements:** 0 detected.  
**Circular patterns detected:** 0 detected.  
**Insufficient assertions:** lifecycle, two-target isolation, issue-cap completeness, and end-to-end repair verification are not asserted.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---:|---|---|---|
| `src/finance_sync/services/wealthfolio_health_bridge.py` | 34-39 | Unsafe purchase-price category checked after generic price category | 🛑 BLOCKER | Routes an unsafe issue into automatic repair. |
| `src/finance_sync/worker/jobs.py` | 120-123 | Bounded list passed as complete health snapshot | 🛑 BLOCKER | Can falsely resolve omitted remote issues. |
| `src/finance_sync/reconciliation/remediation/backlog.py` | 77-91 | Dedup key omits target identity | 🛑 BLOCKER | Cross-target backlog collision. |
| `src/finance_sync/exporter/wealthfolio/client.py` | 237-240 | Generic `RequestError` is not retried | ⚠️ WARNING | Transient transport outages leave stale polling state. |
| `src/finance_sync/services/data_health.py` | 252-254 | `enabled=target_count > 0` | ⚠️ WARNING | Operational status can contradict disabled deployment. |

No unreferenced `TBD`, `FIXME`, or `XXX` debt markers were found in the phase-changed files.

### Human Verification Required

The phase is an infrastructure/service phase, but these explicit external and runtime checks remain necessary after the blockers are fixed:

1. **End-to-end repair lifecycle** — Run against PostgreSQL and a mocked/deployed Wealthfolio target. Expected: a quote/history issue is repaired from canonical data, projected remotely, failed remote verification stays `retry_wait`, and only confirmed health becomes `resolved`.
2. **Control-plane authorization** — Call the health-sync endpoint as a read-only user and as a `sync:write` user. Expected: read-only is denied and the authorized call is bounded and tenant-scoped.
3. **Deployment and real payload smoke test** — Deploy with the bridge disabled, apply migration, then enable one target only. Expected: no secrets/raw payloads appear in logs or metrics, target failures do not affect other tenants, and observed Wealthfolio payloads normalize correctly.

### Gaps Summary

Phase 01 is not achieved. The three blocking review threats remain open: unsafe purchase-price findings can be auto-repaired, separate Wealthfolio targets collide in the backlog, and issue-limit truncation can falsely resolve omitted findings. In addition, canonical identity resolution is incomplete for common health payloads, transport retry coverage is incomplete, and the required database/lifecycle/deployment checks were not executed. The phase must remain at the escalation gate until these gaps are addressed or explicitly overridden by the developer.

---

_Verified: 2026-09-12T16:05:00Z_  
_Verifier: the agent (gsd-verifier)_
