---
phase: 01-wealthfolio-remediation-bridge
status: open_threats
threats_open: 6
asvs_level: 1
block_on: high
audited: 2026-09-12
---

# Phase 01 Security Audit

## Summary

The post-execution security audit found 5 of 12 registered threats mitigated and 6 blocking open threats. Phase advancement is blocked until the high-severity findings are fixed or explicitly accepted and documented.

## Closed Threats

| Threat | Severity | Evidence |
|---|---:|---|
| T-01-08-02 unsafe classification | high | Unsafe-category precedence and unsupported strategy mapping are covered in `wealthfolio_health_bridge.py`. |
| T-01-09-02 retry boundedness | medium | Client retries timeout, transport, and transient HTTP failures with bounded backoff. |
| T-01-09-03 error disclosure | medium | Client errors and worker logs are sanitized and credential-free. |
| T-01-10-02 historical window semantics | medium | Historical projection uses half-open windows. |
| T-01-11-03 validation repudiation | medium | Integration skips record unavailable PostgreSQL/Redis dependencies explicitly. |

## Open Blocking Threats

| Threat | Severity | Required mitigation |
|---|---:|---|
| T-01-08-01 target lookup privilege boundary | high | Use a valid tenant-scoped target identity and exact target lookup; do not rely on the invalid credential-scoped reference. |
| T-01-09-01 incomplete snapshot tampering | high | Treat malformed or bounded/incomplete issue representations as incomplete and prevent disappearance reconciliation. |
| T-01-10-01 canonical identity elevation | high | Validate contradictory remote identifiers against the resolved canonical security before any automatic repair. |
| T-01-10-03 unsafe repair boundary | high | Preserve manual-review-only routing with a valid target persistence contract. |
| T-01-11-01 lifecycle tampering | high | Prove durable verify-before-resolve behavior and prevent omitted findings from incomplete normalization from resolving. |
| T-01-11-02 target-scoped persistence elevation | high | Add valid tenant/target persistence constraints and exact connector routing. |

## Open Non-Blocking Threats

| Threat | Severity | Required mitigation |
|---|---:|---|
| T-01-08-03 payload disclosure | medium | Restrict persisted context to an allowlist instead of truncating arbitrary remote details/message text. |

## Audit 2026-09-12

| Metric | Count |
|---|---:|
| Threats found | 12 |
| Closed | 5 |
| Open | 7 |
| Blocking open | 6 |

## Gate

Phase completion is blocked while `threats_open: 6`. Fix the mitigations and rerun `$gsd-secure-phase 01`, or explicitly accept and document the risks before rerunning the security gate.
