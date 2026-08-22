# Audit-trail completeness

`config/audit-trail-policy.json` is the inventory for security and
configuration mutations. It covers credentials, sync configuration, security
resolution and exports. Each record has actor, timestamp, tenant, object type,
action and a redacted diff. The read API is role-scoped and read-only; records
are retained for 3650 days unless a legal hold requires longer retention.

The exportable synthetic investigation example is
`config/incident-audit-example.json`. Validate it with:

```bash
uv run python scripts/audit_trail_completeness.py
```

Secrets, tokens, credential values and financial fields must never be added to
the diff. Use provider-fixture identifiers and redacted change markers when
investigating an incident.
