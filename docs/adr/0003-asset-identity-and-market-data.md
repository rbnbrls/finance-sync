# ADR 0003: Separate security identity from listings and observations

- Status: Accepted
- Date: 2026-07-20

## Decision

Represent an economic instrument as `security` and a tradable venue/currency symbol as `security_listing`. Store prices, metadata, and fundamentals as time-versioned observations. OpenBB is accessed through an internal gateway and is a market-data enrichment source, not a ledger provider.

## Consequences

Ticker changes and cross-listed instruments do not corrupt holdings. Identity resolution may initially require a review queue for ambiguous mappings. Cache TTLs are data-type-specific and never replace durable observations.
