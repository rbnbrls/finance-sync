# Connector version lifecycle

`config/connector-lifecycle.json` is the lifecycle source of truth. A release
must declare connector version, capabilities, minimum fixture date, feature
flag, deprecation date, removal date and the previous rollback version.

`scripts/connector_lifecycle.py` emits safe diagnostics for health and sync
operations: healthy, disabled, deprecated or incompatible. Existing
connections remain on the current version until the fixture and contract
suite pass; the previous version is retained until then.

Operators should enable the feature flag only after contract CI passes, warn
users before the deprecation date, and remove a connector only after the
published removal date and migration/rollback review. Diagnostics never
contain credentials or financial data.

## Health contract

Provider health has three independent levels: `connection` (credentials and
provider connectivity), `resources` (source-data availability per resource),
and `processing` (the last successful sync). `connected` means only that a
credential/provider check succeeded; it does not imply healthy source data or
successful processing.

Reauthentication tests replacement credentials before atomically encrypting
and activating them. Audit records contain tenant, actor, provider,
connection, timestamp, result and reason code, while credentials, tokens,
headers, financial payloads and complete provider error text are excluded.
Telemetry uses a truncated SHA-256 connection hash and follows the audit
retention policy.

## Rollback runbook

Inspect connection health and release evidence, pause the affected release,
confirm the previous certified version, execute the operator-only rollback,
then resume only after contract, canary and synthetic processing checks pass.
Rollback changes release state only; financial data, cursors and audit history
are not deleted or rewritten.
