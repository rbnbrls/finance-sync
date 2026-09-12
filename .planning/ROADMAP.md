# Finance-sync Roadmap

## Project goal

Build an automatic Wealthfolio → finance-sync data-quality remediation bridge.
Wealthfolio health issues must be discovered, translated into the existing
finance-sync remediation backlog, repaired when safe, and verified in
Wealthfolio.

## Phase 1 — Wealthfolio remediation bridge

**Status:** Planned  
**Plan:** [01-PLAN.md](phases/01-wealthfolio-remediation-bridge/01-PLAN.md)
**Gap-closure plans:** 4

Gap-closure plans:

- [x] 01-08-PLAN.md — target-scoped backlog identity and unsafe classification
- [x] 01-09-PLAN.md — complete-snapshot lifecycle guards and operational truth
- [x] 01-10-PLAN.md — canonical identity and half-open historical repair
- [ ] 01-11-PLAN.md — persisted lifecycle and migration validation

### Outcome

The scheduled finance-sync worker polls authenticated Wealthfolio targets,
deduplicates and queues Wealthfolio health issues, repairs safe quote and
historical-price issues, sends the repaired data back to Wealthfolio, and
marks items resolved only after Wealthfolio health confirms the repair.

### Scope

- Authenticated Wealthfolio `/api/v1/health/status` polling.
- Stable health-issue normalization and backlog deduplication.
- Quote and historical-price remediation.
- Manual-review routing for unsafe transaction and valuation issues.
- Targeted Wealthfolio quote/export synchronization.
- Polling state, metrics, logs, API trigger, tests, and deployment flags.

### Success criteria

- Repeated polling does not create duplicate remediation items.
- Wealthfolio quote issues appear in `data_quality_remediation_items`.
- Safe quote repairs update canonical finance-sync data and Wealthfolio.
- Resolved issues become `resolved` only after remote verification.
- Missing purchase prices and negative valuations are never fabricated; they
  become `manual_review` with actionable context.
- Auth failures, rate limits, retries, and unknown health categories are
  observable and tenant-safe.
- The bridge is disabled by default until deployment configuration is verified.

### Dependencies

- Existing `ExportTarget` Wealthfolio credentials and client.
- Existing remediation backlog, scheduler, enrichment gateway, and exporter.
- A deployed remediation worker with `REMEDIATION_ENABLED=true` or the
  equivalent data-quality repair worker setting.
