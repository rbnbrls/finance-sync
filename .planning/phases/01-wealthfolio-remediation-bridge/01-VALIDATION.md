---
phase: "01"
slug: "wealthfolio-remediation-bridge"
status: validated
nyquist_compliant: false
wave_0_complete: false
created: "2026-09-12"
---

# Phase 01 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e"` |
| **Full suite command** | `make test` |
| **Estimated runtime** | ~60 seconds |

## Sampling Rate

- **After every task commit:** Run the targeted pytest command for the changed subsystem.
- **After every plan wave:** Run `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e"`.
- **Before `$gsd-verify-work`:** Full suite must be green.
- **Max feedback latency:** 120 seconds.

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 1 | Phase success criteria | T-01 / — | Credentials and payloads are not logged; transient health failures are bounded and classified | unit | `uv run pytest tests/test_phase01_wealthfolio_bridge.py -q -k 'health_poll'` | ✅ | ✅ green |
| 01-01-02 | 01 | 1 | Deduplicated remediation items | T-02 / — | Tenant and target boundaries remain isolated; affected entities remain independently traceable | unit | `uv run pytest tests/test_phase01_wealthfolio_bridge.py -q -k health_findings` | ✅ | ⚠ escalated |
| 01-01-03 | 01 | 2 | Safe quote/history repair | T-03 / — | Remote verification rejects unowned quotes; unsafe findings route to manual review | unit/integration | `uv run pytest tests/test_phase01_wealthfolio_bridge.py -q -k quote_repair` | ✅ | ⚠ warning |
| 01-01-04 | 01 | 3 | Scheduler/API/deployment controls | T-04 / — | Bridge remains disabled by default | unit | `uv run pytest tests/test_phase01_wealthfolio_bridge.py -q -k bridge_is_disabled` | ✅ | ✅ green |

## Wave 0 Requirements

- [x] Add fixtures for the exact Wealthfolio health-status payload and transient/error responses.
- [x] Add test doubles for authenticated Wealthfolio health polling and remote verification.
- [ ] Add migration-test coverage for target-scoped bridge state (test exists; skipped without PostgreSQL).

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real deployed Wealthfolio health payload compatibility | Phase success criteria | Requires a deployed Wealthfolio instance and credentials | Deploy with the bridge disabled, run migrations, perform a bounded manual poll, and confirm the observed payload is parsed without credential or raw-payload leakage. |
| Safe staged enablement | Bridge disabled by default | Deployment configuration and worker registration are environment-specific | Verify the flag is false in the default deployment, then enable it for one target and observe poll, repair, and verification metrics before expanding scope. |
| PostgreSQL cursor migration | Cursor rows are isolated by tenant and target | `TEST_DATABASE_URL` is not configured in this checkout; the integration contract was collected but skipped | Run `APP_ENVIRONMENT=dev DEBUG=false TEST_DATABASE_URL=... TEST_REDIS_URL=... uv run pytest -m integration tests/integration/test_phase01_wealthfolio_migration.py -q`. |
| End-to-end safe repair lifecycle | Quote failure → backlog → canonical repair → remote verification | No end-to-end fixture/service was available for this audit | Run the existing remediation worker integration suite against PostgreSQL and a mocked Wealthfolio target; assert failed verification remains `retry_wait`. |
| Control-plane authorization | Read-only users cannot trigger health polling | No authenticated API fixture was available in the focused audit | Exercise `POST /api/v1/control-plane/wealthfolio/health-sync` as read-only and sync-write users; expect denial and bounded tenant-scoped execution respectively. |

## Validation Audit 2026-09-12

| Metric | Count |
|--------|-------|
| Gaps found | 4 |
| Resolved | 2 |
| Escalated implementation gaps | 1 |
| Environment/manual-only gaps | 1 |

### Executed Results

- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest tests/test_phase01_wealthfolio_bridge.py -q`: **10 passed, 1 failed**.
- The failing test is intentionally retained: `MISSING_PURCHASE_PRICE` is classified as `wealthfolio_quote_sync_failure`, contradicting the plan's manual-review mapping. This is an implementation defect and is not fixed in this validation pass.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -m integration tests/integration/test_phase01_wealthfolio_migration.py -q`: **1 skipped** because `TEST_DATABASE_URL`/PostgreSQL is unavailable.
- `PYTHONPATH=src python3 -m compileall -q` over the new tests: **passed**.

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter — blocked by the escalated classifier defect and unavailable PostgreSQL integration run

**Approval:** partial; implementation escalation required
