# ADR 0002: Canonical facts with encrypted raw-payload retention

- Status: Accepted
- Date: 2026-07-20

## Decision

Store provider-independent canonical facts as the source for all APIs/exporters, while retaining encrypted raw payloads, fingerprints, schema version, and provider identity for audit and reprocessing. Providers never expose their DTOs outside connector/normalization modules.

## Consequences

Consumers have a stable contract; connector upgrades can be replayed. Retention is configurable because raw data is sensitive and storage-expensive. Deletion/anonymization processes must remove or cryptographically render unreadable both raw payloads and derived PII where required.
