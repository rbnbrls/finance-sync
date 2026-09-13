# Phase 1: wealthfolio-remediation-bridge - Context

**Gathered:** 2026-09-12
**Status:** Ready for gap-closure planning

<domain>
## Phase Boundary

Close the verified safety and correctness gaps in the Wealthfolio remediation bridge: preserve target-scoped backlog identity, distinguish complete from capped health snapshots, keep unsafe financial findings manual-only, resolve canonical asset identity before automatic repair, align historical date windows, and report retry/operational status truthfully. The phase scope remains the existing roadmap scope; no new capabilities are added.

</domain>

<decisions>
## Implementation Decisions

### Target identity and deduplication
- **D-01:** Use the existing `connection_id` field as the stable Wealthfolio target identity in backlog deduplication and repair routing.
- **D-02:** Preserve the target database ID across credential or configuration rotation; do not derive identity from mutable endpoint or account fields.
- **D-03:** Require each remediation strategy and connector factory to use the exact target connection recorded on the backlog item.
- **D-04:** Preserve legacy backlog items and backfill their target connection where possible; route ambiguous legacy records to `manual_review` rather than guessing.

### Health snapshot completeness
- **D-05:** Treat responses capped by the per-cycle issue limit as incomplete; never resolve omitted items from an incomplete snapshot.
- **D-06:** Persist a `complete`/`truncated` indicator with cursor state and metrics so deferred reconciliation is durable and observable.
- **D-07:** Prefer bounded pagination or a complete server-side fetch when supported; otherwise retain the incomplete-snapshot guard.
- **D-08:** A failed poll leaves previously active remediation items unchanged for retry; failure must never imply issue disappearance.

### Safe repair boundaries
- **D-09:** Resolve remote asset IDs through the existing canonical security lookup. If identity cannot be proven, route to `manual_review`.
- **D-10:** Preserve explicit `identifier_type` for ISIN, ticker, and provider-specific symbols and use the matching canonical lookup path.
- **D-11:** Purchase/cost-basis, negative or incomplete valuation, transaction/transfer, and unknown categories remain `manual_review` only. Automatic repair is limited to safe quote and historical-price gaps.
- **D-12:** Use a consistent half-open `[start, end)` date interval for historical projection and remote verification, normalizing date-only end values to the next day.

### Operational truth
- **D-13:** Retry transient `httpx.RequestError` failures with bounded backoff while failing fast on configuration and authentication errors.
- **D-14:** Data Health reports the feature flag as `enabled`; target availability, last successful poll, errors, and incomplete snapshots are separate signals.
- **D-15:** Partial outages do not flip the overall enabled flag; affected targets and degraded state remain visible.
- **D-16:** When dependencies are unavailable during validation, record the result as explicitly unverified/skipped and retain the manual verification checkpoint.

### the agent's Discretion
- Exact field names for the persisted completeness indicator, provided the complete/incomplete semantics are durable and observable.
- Exact canonical lookup helper and migration/backfill mechanics, provided they follow the existing repository patterns and fail closed.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase contract and findings
- `.planning/ROADMAP.md` — Phase 1 scope, outcome, dependencies, and success criteria.
- `.planning/phases/01-wealthfolio-remediation-bridge/01-VERIFICATION.md` — authoritative verified gaps and required fixes.
- `.planning/phases/01-wealthfolio-remediation-bridge/01-REVIEW.md` — adversarial code-review findings CR-01 through CR-03 and warnings.
- `.planning/phases/01-wealthfolio-remediation-bridge/01-RESEARCH.md` — implementation patterns, pitfalls, and Wealthfolio integration assumptions.
- `.planning/phases/01-wealthfolio-remediation-bridge/01-VALIDATION.md` — validation map and environment limitations.

### Existing implementation seams
- `src/finance_sync/services/wealthfolio_health_bridge.py` — health normalization, backlog registration, and disappearance reconciliation.
- `src/finance_sync/reconciliation/remediation/backlog.py` — `DetectedIssue` identity and deduplication contract.
- `src/finance_sync/worker/jobs.py` — polling limits, cursor updates, and failure handling.
- `src/finance_sync/reconciliation/remediation/wealthfolio.py` — safe quote/history strategies and remote verification.
- `src/finance_sync/exporter/wealthfolio/client.py` — authenticated health polling and retry behavior.
- `src/finance_sync/services/data_health.py` — bridge operational status projection.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Existing `DetectedIssue` and `BacklogRepository` abstractions provide the shared deduplication and lifecycle transition path.
- Existing encrypted `ExportTarget` credentials and Wealthfolio client must remain the credential boundary.
- Existing canonical security lookup, quote enrichment, exporter projection, and strategy verification paths should be reused rather than duplicated.

### Established Patterns
- Tenant and connection-scoped remediation items use `connection_id` in the shared backlog identity.
- Health polling already maintains per-target cursor state and bounded issue context; completeness must extend that state rather than storing raw payloads.
- Automatic repair is fail-closed: unproven identity or unsafe financial categories go to `manual_review`.

### Integration Points
- The scheduled worker feeds normalized findings into the remediation backlog and cursor state.
- The remediation executor selects strategies from backlog context and must preserve the exact target connection.
- Data Health and metrics expose rollout and degraded-state signals without credentials or raw remote payloads.

</code_context>

<specifics>
## Specific Ideas

- The user's choices consistently favor preserving existing repository seams and adding explicit safety boundaries over introducing new abstractions.
- The gap-closure plan should include regression tests for two targets, capped snapshots, unsafe purchase-price routing, canonical identity resolution, and failed remote verification.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-wealthfolio-remediation-bridge*
*Context gathered: 2026-09-12*
