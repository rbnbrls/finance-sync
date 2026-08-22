# Audit retention enforcement

`config/audit-retention-policy.json` is the versioned policy: audit records
are tenant-scoped, retained for 3650 days, archived before deletion and
processed in batches of 500. Dry-run is the default operator mode.

Example:

```bash
uv run python scripts/enforce_audit_retention.py \
  --tenant tenant-fixture --dry-run
```

The deletion run emits only tenant and record-count metadata under the
`audit_retention.run` audit event. A storage failure rolls back records already
deleted in that run; rerunning after the failure is supported. This release
changes policy enforcement only and therefore requires no schema migration.
