---
phase: 01-wealthfolio-remediation-bridge
reviewed: 2026-09-12T17:19:33Z
depth: deep
files_reviewed: 26
files_reviewed_list:
  - .env.example
  - coolify.yaml
  - docs/wealthfolio-remediation-bridge.md
  - migrations/versions/0071_wealthfolio_health_cursors.py
  - migrations/versions/0072_backfill_wealthfolio_remediation_target_identity.py
  - src/finance_sync/api/v1/control_plane.py
  - src/finance_sync/config/settings.py
  - src/finance_sync/exporter/wealthfolio/__init__.py
  - src/finance_sync/exporter/wealthfolio/client.py
  - src/finance_sync/models/__init__.py
  - src/finance_sync/models/wealthfolio_health_cursor.py
  - src/finance_sync/observability/metrics.py
  - src/finance_sync/reconciliation/remediation/executor.py
  - src/finance_sync/reconciliation/remediation/price_history.py
  - src/finance_sync/reconciliation/remediation/wealthfolio.py
  - src/finance_sync/schemas/data_health.py
  - src/finance_sync/services/data_health.py
  - src/finance_sync/services/wealthfolio_health_bridge.py
  - src/finance_sync/worker/jobs.py
  - src/finance_sync/worker/scheduler.py
  - tests/exporter/test_wealthfolio_client.py
  - tests/fixtures/wealthfolio_health_status.json
  - tests/integration/test_phase01_legacy_remediation_backfill.py
  - tests/integration/test_phase01_wealthfolio_lifecycle.py
  - tests/integration/test_phase01_wealthfolio_migration.py
  - tests/test_phase01_wealthfolio_bridge.py
  - tests/test_wealthfolio_health_bridge.py
  - tests/test_wealthfolio_remediation_contract.py
findings:
  critical: 5
  warning: 2
  info: 0
  total: 7
status: issues_found
---

# Phase 01: Code Review Report

**Reviewed:** 2026-09-12T17:19:33Z  
**Depth:** deep  
**Files Reviewed:** 26  
**Status:** issues_found

## Summary

The review traced the health response through normalization, durable backlog persistence, target connector construction, remediation execution, and remote verification. Unit tests pass (76 passed), but all four PostgreSQL/Redis integration tests are skipped in this environment, so the persistence and migration contracts remain unverified. The implementation has ship-blocking defects in the target identity schema, pagination/completeness accounting, canonical identity binding, and sensitive payload handling.

## Critical Issues

### CR-01: Wealthfolio target IDs violate the remediation foreign key

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:338-350`; `src/finance_sync/models/remediation.py:72-75`; `migrations/versions/0068_add_data_quality_remediation_backlog.py:77-79`  
**Issue:** New Wealthfolio findings persist `ExportTarget.id` as `DetectedIssue.connection_id`, and the remediation worker later resolves that value from `export_targets`. However, `data_quality_remediation_items.connection_id` still references `credentials.id` (in both the ORM model and the migration). On PostgreSQL, inserting a normal target UUID therefore fails the FK constraint, so polling cannot enqueue work; migration 0072 also attempts to write target UUIDs into the same credentials FK column. The lifecycle/backfill integration fixtures reproduce this invalid relationship but are skipped, masking the failure.  
**Fix:** Give target-scoped remediation identity its own strongly typed column/FK (for example `target_id` referencing `export_targets.id`) and use it consistently in deduplication, the connector factory, and reconciliation; or change the schema contract and all existing credential consumers so one column cannot ambiguously reference two tables. Add an executed PostgreSQL migration test that inserts an actual target-backed remediation row and runs 0072.

### CR-02: Recorded pagination cursors are never consumed

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:74-99`; `src/finance_sync/worker/jobs.py:125-138`; `src/finance_sync/exporter/wealthfolio/client.py:211-279`  
**Issue:** `prepare_health_poll()` records `nextCursor`/`hasMore` and marks the snapshot incomplete, but the client has no cursor argument and the worker performs exactly one request per target. The next cursor is never sent back to Wealthfolio, so a paginated response permanently imports only its first page. This avoids false disappearance resolution but silently leaves later findings untracked and unrepairable on every future poll.  
**Fix:** Implement a bounded page-drain loop that passes the cursor to the health endpoint, detects repeated cursors, and marks the aggregate incomplete if the configured page/issue budget is exhausted. Persist the cursor only when continuation is required, and reconcile only after all pages have been collected successfully.

### CR-03: Dropped issue entries are treated as a complete snapshot

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:63-99,245-276,332-350`  
**Issue:** Completeness is inferred only from `issues` being a list and its top-level length being within `issue_limit`. Normalization silently skips non-dict entries and truncates both the issue list and each issue's `affectedItems` to `MAX_AFFECTED_ITEMS`. A response containing one valid issue plus malformed entries, or one issue with more than 100 affected assets, is therefore passed to `enqueue_success()` as `complete=True`; the active-query loop then resolves previously active items that were merely omitted by normalization.  
**Fix:** Make normalization return a completeness/truncation result (or validate before reconciliation), mark the snapshot incomplete whenever any issue/affected-item entry is rejected or bounded, and carry that flag into `enqueue_success()`. Do not reconcile disappearance unless every provider entry was represented, or explicitly paginate/batch the affected items.

### CR-04: Explicit remote identifiers are not checked against the resolved security

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:178-224,227-242`; `src/finance_sync/reconciliation/remediation/quote.py:44-51`  
**Issue:** When a finding supplies `security_id`, `_find_canonical_security()` verifies only that the local security UUID exists. `_canonical_identifier()` then preferentially trusts the finding's independent `identifier`/`identifier_type` without checking that it belongs to that security. A malformed or compromised Wealthfolio response can pair security A's ID with security B's ticker; the enrichment gateway fetches B while storing the result under A, and the bridge can then push that wrong quote to the remote asset.  
**Fix:** Treat the database-resolved security as authoritative: use an identifier read from that row (or a listing explicitly linked to it), and only accept a supplied identifier after a matching canonical lookup proves the same security. Reject contradictory `security_id`/identifier pairs and route them to `manual_review`.

### CR-05: Provider diagnostic text is persisted and exposed without secret redaction

**Classification:** BLOCKER  
**File:** `src/finance_sync/services/wealthfolio_health_bridge.py:289-295`; `src/finance_sync/schemas/remediation.py:22-44`; `src/finance_sync/api/v1/control_plane.py:121-175`  
**Issue:** The model contract says raw provider responses must not enter `context`, but normalization copies arbitrary Wealthfolio `details`/`message` and `fixAction` text into the persisted JSON context (only truncating to 256/64 characters). The remediation API returns that context to any tenant principal with reconciliation-read permission. If the health endpoint includes a URL, token, account data, or other sensitive diagnostic content, it is stored and disclosed; truncation is not sanitization.  
**Fix:** Persist only an allowlisted set of typed identifiers and bounded reason codes. Redact credential-shaped values, URLs with query/userinfo, authorization headers, and account/asset payloads before storage, or omit provider text entirely and keep sanitized aggregates. Add a test proving secret-like fields in details/message never appear in the database or API response.

## Warnings

### WR-01: Malformed historical dates escape before executor error handling

**Classification:** WARNING  
**File:** `src/finance_sync/reconciliation/remediation/price_history.py:163-175`; `src/finance_sync/reconciliation/remediation/wealthfolio.py:79-92`; `src/finance_sync/reconciliation/remediation/executor.py:100-113`  
**Issue:** `_date()` lets `datetime.fromisoformat()` raise for an arbitrary Wealthfolio date string. The executor calls `strategy.supports(item)` before entering its `try` block, so a malformed historical finding aborts the batch/tenant path instead of transitioning the claimed item to retry or manual review. Because the claim was committed before execution, the row can remain `processing` until lease expiry and repeat indefinitely.  
**Fix:** Make date parsing return `None` for `TypeError`/`ValueError`, have `supports()` fail closed, and/or move support evaluation inside the executor's guarded transition path so invalid input becomes `manual_review` with a sanitized error category.

### WR-02: Authentication failure bookkeeping targets the wrong table for Wealthfolio

**Classification:** WARNING  
**File:** `src/finance_sync/reconciliation/remediation/executor.py:213-221,265-271`; `src/finance_sync/reconciliation/remediation/backlog.py:365-389`  
**Issue:** The executor invokes `mark_connection_auth_failure()` for every provider authentication error, but that helper updates `Credential` by `connection_id`. Wealthfolio remediation connections are `ExportTarget` IDs by design, so a Wealthfolio auth failure does not mark the target as requiring reauthentication (and can only update an unrelated credential if UUIDs ever coincide). This leaves the target repeatedly retryable with no actionable lifecycle state.  
**Fix:** Dispatch auth-failure bookkeeping by connection kind: update the Wealthfolio `ExportTarget` health/error fields or add a target-specific reauthentication state, while retaining the existing credential path for credential-backed providers. Ensure the remediation outcome and target health transition are committed together.

---

_Reviewed: 2026-09-12T17:19:33Z_  
_Reviewer: the agent (gsd-code-reviewer)_  
_Depth: deep_
