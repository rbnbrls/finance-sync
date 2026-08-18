# ADR 0004: Versioned Personal Finance Core contract

- Status: Proposed
- Date: 2026-08-18

## Context

finance-sync needs a stable data contract for multiple personal-finance
providers and self-hosted downstream tools. OpenBB ODP is valuable for market
data but does not define the required personal-finance ledger and custody
model.

## Decision

Adopt [Personal Finance Core v1](../personal-finance-core-v1.md) as the
provider-neutral domain contract. It owns personal-finance facts, source
provenance, versioning, and datamart boundaries. OpenBB is used exclusively as
an optional instrument, price, FX, and metadata enrichment adapter.

## Consequences

The project gains explicit semantics for cash transactions, investment
activities, cash positions, snapshots, and consumer compatibility. It requires
a staged dual-write migration, a lineage store, and governed datamart grants
before the legacy models can be retired.
