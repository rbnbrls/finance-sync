# Control-plane contract

The control plane is a tenant-scoped read projection over the canonical
dataset and operational records. It does not introduce a second source of
truth for financial data or operational state.

## Ownership and timestamps

| Projection | Source | Ownership | Operational timestamp |
| --- | --- | --- | --- |
| connections | `Credential` | `Credential.tenant_id` | last attempt/success/test |
| syncs | `SyncRun` joined to `Credential` | credential tenant | started/completed |
| unresolved securities | `UnresolvedSecurity` | `UnresolvedSecurity.tenant_id` | created/updated |
| freshness | `EnrichmentFreshness` via tenant holdings | holding tenant | quote/enrichment update |
| coverage | `Account`, `Transaction` | tenant | latest canonical observation |
| destinations | `ExportTarget` | `ExportTarget.tenant_id` | health/export/schedule |
| exports | `ExportRun` | `ExportRun.tenant_id` | started/completed |
| reconciliation | `ReconciliationRun/Result` | run/result tenant | completed/started |

`overview.as_of` is the newest reliable timestamp from all visible
projections, including export and reconciliation runs. `generated_at` is the
time at which the projection was built.

## Statuses and actions

Sync statuses are rendered as Bezig, Voltooid, Mislukt, Gedeeltelijk,
Overgeslagen and Geannuleerd. Freshness uses `fresh`, `stale`, `partial` and
`unavailable`. Overview status priority is `sync_failed`, then
`attention_required`, then `partial`, otherwise `healthy`.

Every issue exposes exactly one action from the allow-listed action catalog.
The backend sets `enabled` and `disabled_reason` from the authenticated
permissions and current lifecycle state. Mutating retry actions are
single-flight when Redis is configured and are tenant-scoped.

## Security mapping compatibility

The preferred mapping contract is `PUT /api/v1/securities/{security_id}/map`
with provider and external identifiers in the body. The existing
`PUT /api/v1/securities/map` body-target form remains as a backwards-compatible
alias. Both routes require `securities:write` and resolve only within the
authenticated tenant.

## Export recovery

Export runs carry `target_id`, `account_scope` and sanitized
`delivery_checkpoint` metadata. Destination retry is available only when the
latest tenant-scoped run failed; it reuses the destination configuration and
the exporter's delivery cursor.

## Persistence boundary

Issues remain derived from domain state until acknowledgement, snoozing or
assignment is required. At that point a `control_plane_issues` table should
be introduced with tenant, category, severity, fingerprint, lifecycle status,
timestamps and a sanitized payload.
