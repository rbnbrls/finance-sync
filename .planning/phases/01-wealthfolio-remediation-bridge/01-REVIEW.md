---
phase: 01-wealthfolio-remediation-bridge
reviewed: 2026-09-12T15:40:00Z
depth: standard
files_reviewed: 17
files_reviewed_list:
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
findings:
  critical: 3
  warning: 4
  info: 0
  total: 7
status: issues_found
---

# Phase 01: Code Review Report

**Reviewed:** 2026-09-12T15:40:00Z  
**Depth:** standard  
**Files Reviewed:** 17  
**Status:** issues_found

## Summary

The bridge has several correctness failures in issue classification, target isolation, and lifecycle reconciliation. The most serious paths can automatically process a purchase-price finding, merge two Wealthfolio targets into one backlog item, or resolve findings that were merely omitted by the configured response cap. Safe quote/history remediation also cannot reliably derive canonical identities from the normalized health payload.

## Critical Issues

### CR-01: Purchase-price findings are routed to automatic quote repair

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:29-39`  
**Issue:** `_issue_kind()` checks for the substring `price` before checking `purchase`/`cost_basis`. A normal code such as `MISSING_PURCHASE_PRICE` therefore matches the first branch and becomes `wealthfolio_historical_price_gap` (because it also contains `missing_price`), or otherwise `wealthfolio_quote_sync_failure`. `_strategy()` then assigns an automatic repair strategy. This violates the phase contract that missing purchase prices must always become `manual_review`; the repair can mutate/project a quote for a cost-basis issue and mark the wrong finding resolved.
**Fix:** Match unsafe categories first and use exact normalized codes/categories where possible, for example:

```python
if "purchase" in raw or "cost_basis" in raw:
    return "wealthfolio_missing_purchase_price"
if "negative" in raw and "valuation" in raw:
    return "wealthfolio_negative_valuation"
if "incomplete" in raw and "valuation" in raw:
    return "wealthfolio_incomplete_valuation"
if any(token in raw for token in ("quote", "price", "market_data")):
    ...
```

### CR-02: Backlog deduplication merges separate Wealthfolio targets

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:145-149` (deduplication contract: `src/finance_sync/reconciliation/remediation/backlog.py:77-89`)  
**Issue:** Findings carry `target_id` only in `context`, and the bridge leaves `connection_id` and `scope` unset. The shared deduplication key excludes context, so the same tenant, issue type, entity type, and entity ID on two active Wealthfolio targets produce the same key. Registering the second target updates the first target's item context to the second target, and remediation then operates against the wrong destination. This is a tenant-local cross-target isolation failure and also breaks independent lifecycle resolution.
**Fix:** Include the target identity in the deduplication identity, preferably by setting a stable `scope` or `connection_id` on `DetectedIssue` and retaining the tenant/target check in the connector factory. Add a regression test with two targets in one tenant asserting two backlog rows and two separately routed repairs.

### CR-03: The issue cap causes false resolution of omitted findings

**Classification:** BLOCKER  
**File:** `src/finance_sync/worker/jobs.py:120-123` and `src/finance_sync/services/wealthfolio_health_bridge.py:169-188`  
**Issue:** The worker truncates the remote `issues` array to `WEALTHFOLIO_HEALTH_BRIDGE_ISSUE_LIMIT`, then passes the truncated payload to `enqueue_success()`. That method treats the supplied list as the complete successful snapshot and resolves every active Wealthfolio item not in it. When Wealthfolio reports more issues than the cap, all omitted items are incorrectly marked `resolved` even though they remain remote problems.
**Fix:** Preserve a `truncated`/`complete` flag and skip disappearance reconciliation whenever the response was capped; alternatively paginate/fetch the complete issue set before reconciling. Record the cap in cursor/metrics so operators know that lifecycle reconciliation was deferred.

## Warnings

### WR-01: Normalization does not perform the promised canonical identity resolution

**Classification:** WARNING  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:81-109` and `src/finance_sync/reconciliation/remediation/wealthfolio.py:27-35,78-85`  
**Issue:** The bridge copies `securityId`, `isin`, and `ticker` into context but never resolves a Wealthfolio asset to a finance-sync `security_id`. The remediation strategies require `security_id`, so common payloads containing only an asset `id` (the shape covered by the added contract test) are immediately routed to `manual_review` rather than repaired. When an ISIN is copied into `identifier`, `identifier_type` is not set, so the canonical strategy defaults to treating that ISIN as a ticker. This makes the advertised safe quote/history path fail or query the wrong provider identity.
**Fix:** Resolve the remote asset using the canonical security lookup in the bridge before registering a repair finding; store both `security_id` and the correct `identifier_type`. If identity cannot be proven, explicitly keep the item manual-review and expose the reason rather than registering it with an automatic strategy.

### WR-02: Historical repair uses an inclusive end bound while verification is exclusive

**Classification:** WARNING  
**File:** `src/finance_sync/reconciliation/remediation/wealthfolio.py:92-100` and `src/finance_sync/reconciliation/remediation/price_history.py:124-129`  
**Issue:** The projection query includes `timestamp <= end`, while the shared verification query requires `timestamp < end`. For a health range whose `endDate` is a date at midnight, observations on the requested end date can be projected but are excluded from verification. The item then retries or eventually becomes `manual_review` despite a successful projection.
**Fix:** Define one interval convention and use it in both projection and verification. For date-based ranges, normalize `endDate` to the next day and use a half-open `[start, end)` window consistently.

### WR-03: Transient transport failures are not retried

**Classification:** WARNING  
**File:** `src/finance_sync/exporter/wealthfolio/client.py:223-240`  
**Issue:** The health client retries timeouts and transient HTTP statuses, but immediately raises on every other `httpx.RequestError`. DNS failures, connection resets, and temporary network errors are common transient polling failures and are explicitly within the phase's retry/error-normalization contract. This produces avoidable failed polls and stale cursors instead of bounded retry behavior.
**Fix:** Apply the same bounded backoff to retryable `RequestError` subclasses (or retry the whole transient transport category), while preserving immediate failure for non-retryable configuration errors and keeping error messages sanitized.

### WR-04: Data Health reports the bridge enabled when configuration disabled it

**Classification:** WARNING  
**File:** `src/finance_sync/services/data_health.py:252-255`  
**Issue:** The `enabled` field is computed as `target_count > 0`, not from `Settings.wealthfolio_health_bridge_enabled`. With an active target and the default disabled configuration, the API says the bridge is enabled even though the scheduler/job will immediately skip polling. This gives operators an incorrect rollout status and can lead them to assume health data is current.
**Fix:** Inject the feature flag into `DataHealthService` (as is done for other operational settings) and return that value; keep target availability as a separate field/status signal.

---

_Reviewed: 2026-09-12T15:40:00Z_  
_Reviewer: the agent (gsd-code-reviewer)_  
_Depth: standard_
