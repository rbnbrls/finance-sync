# Phase 1: wealthfolio-remediation-bridge - Discussion Log

> **Audit trail only.** Decisions are captured in CONTEXT.md.

**Date:** 2026-09-12
**Phase:** 01-wealthfolio-remediation-bridge
**Areas discussed:** Target identity and deduplication, Health snapshot completeness, Safe repair boundaries, Operational truth

## Target identity and deduplication

| Option | Description | Selected |
|--------|-------------|----------|
| Existing `connection_id` | Reuse the existing backlog identity field for the stable Wealthfolio target ID | ✓ |
| Dedicated `target_id` | Add a new explicit target field across schema and repositories | |
| `scope` only | Encode target identity in scope with minimal schema impact | |

**User's choice:** Use `connection_id`; preserve stable target IDs; route repairs to the exact target; backfill legacy items where possible and send ambiguous records to `manual_review`.

## Health snapshot completeness

| Option | Description | Selected |
|--------|-------------|----------|
| Guard incomplete snapshots | Never resolve omitted items from capped responses; persist completeness state | ✓ |
| Resolve returned items | Treat returned list as authoritative and defer the rest | |
| Fetch all only | Remove local cap or require complete fetches | |

**User's choice:** Prefer bounded pagination when available, otherwise persist `truncated`/`complete`; failed polls leave existing items unchanged.

## Safe repair boundaries

| Option | Description | Selected |
|--------|-------------|----------|
| Canonical lookup and fail closed | Resolve asset identity and route unproven/unsafe categories to `manual_review` | ✓ |
| Trust remote IDs | Treat Wealthfolio asset IDs as finance-sync IDs | |
| Heuristic fallback | Use identifiers without explicit type or broad automation | |

**User's choice:** Preserve `identifier_type`; keep purchase/cost-basis, valuation, transaction/transfer, and unknown categories manual-only; use `[start, end)` intervals.

## Operational truth

| Option | Description | Selected |
|--------|-------------|----------|
| Bounded transient retry and separate status | Retry transport errors and distinguish enabled from target health | ✓ |
| Narrow retries | Retry only timeouts and 5xx responses | |
| Combined or hidden status | Collapse or omit degraded state | |

**User's choice:** Retry transient `httpx.RequestError`; report the feature flag separately from target availability and preserve explicit unverified/skipped checkpoints.

## the agent's Discretion

- Exact persisted field names and lookup/backfill mechanics, subject to the locked safety semantics and existing repository patterns.

## Deferred Ideas

None.
