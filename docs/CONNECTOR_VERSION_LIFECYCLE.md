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
