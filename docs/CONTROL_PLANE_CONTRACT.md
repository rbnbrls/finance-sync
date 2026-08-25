# Control-plane contract

Dit document legt de beslissingen uit Fase 0 vast. Het is de referentie voor
schemas, aggregatie, API-tests en latere GUI-acties.

## Projectievelden

| Projectie | Bron | Belangrijkste timestamps | Scope |
|---|---|---|---|
| Installation | settings, database | `generated_at` | request/tenant |
| Connection | `Credential`, `SyncSchedule` | `last_attempt_at`, `last_success_at`, `next_run_at` | tenant via `Credential.tenant_id` |
| Sync | `SyncRun` via `Credential` | `started_at`, `completed_at`, `cursor` | tenant via `connection_id` |
| Security issue | `UnresolvedSecurity`, canonical `Security` candidates, provider impact counts | `created_at`, `updated_at` | tenant via `UnresolvedSecurity.tenant_id` |
| Freshness | `Holding`, `EnrichmentFreshness` | `updated_at`, `last_quote_fetch` | tenant via holdings |
| Coverage | `Account`, connectorrelaties | accountdata | tenant via `Account.tenant_id` |
| Destination | `ExportTarget`, `SyncSchedule` | health-check en schedule timestamps | `ExportTarget.tenant_id` |
| Export issue | `ExportRun` | `started_at`, `completed_at` | tenant via `ExportRun.tenant_id` |
| Reconciliation | reconciliation runs/findings | `started_at`, `completed_at`, finding timestamps | tenant via reconciliationmodel |

Security mapping gebruikt bewust het bestaande endpoint
`PUT /api/v1/securities/map` met `provider_key`, `external_security_id` en
`target_security_id`. De control-planeactie verwijst naar dit endpoint; de
`/{security_id}/map`-vorm uit het oorspronkelijke backlog is niet aanwezig in
de bestaande API en wordt daarom niet geïntroduceerd naast het huidige,
tenant-veilige contract.

`generated_at` is het moment waarop de overview wordt opgebouwd. `as_of` is de
nieuwste betrouwbare bron-timestamp die daadwerkelijk in de projectie is
gebruikt. Bij afwezigheid van brondata is `as_of = null`; `generated_at` blijft
wel gevuld.

## Statuswaarden

### Sync

De publieke control-planevocabulaire is:

`running`, `completed`, `failed`, `partial`, `skipped`, `cancelled`.

De huidige persistence-laag ondersteunt nog niet iedere waarde als enum; de
projectielaag moet onbekende/legacywaarden niet stilzwijgend als `completed`
interpreteren. Uitbreiding van persistence en retrygedrag valt onder Fase 2.

### Export

`running`, `completed`, `failed`, `cancelled`.

### Freshness

`fresh`, `stale`, `partial`, `unavailable`.

### Overview

`healthy`, `attention_required`, `sync_failed`, `partial`.

De vaste prioriteit is:

1. `sync_failed` wanneer er minstens één mislukte sync is;
2. `attention_required` wanneer er open issues of mislukte destinations zijn;
3. `partial` wanneer freshness `partial` of `unavailable` is;
4. `healthy` in alle overige gevallen.

## Foutcategorieën

Alle operationele foutprojecties gebruiken uitsluitend:

`authentication`, `provider_unavailable`, `rate_limited`, `validation`,
`data_mapping`, `database`, `unknown`.

Een API-response bevat nooit secrets, stack traces of ongesaneerde provider-
of databasefouten. Foutclassificatie en persistence-uitbreiding worden in
Fase 2 verder geharmoniseerd.

## Actiecontract

Iedere `ControlPlaneIssue` bevat precies één `ControlPlaneAction`. De actie
heeft een allow-listed `key`, HTTP-methode, API-pad, permissie, destructive-
vlag en eventueel een disabled reason. De backend bepaalt of de actie enabled
is; de GUI mag die beslissing niet zelfstandig reconstrueren.

## Testmatrix Fase 0

| Situatie | Verwacht resultaat |
|---|---|
| Lege database | geldig contract, `healthy`, `as_of = null` |
| Mislukte sync plus andere issues | `sync_failed` |
| Open issue zonder mislukte sync | `attention_required` |
| Alleen incomplete freshness | `partial` |
| Geen issues en volledige freshness | `healthy` |
| Alleen `None` timestamps | `as_of = null` |
| Gemengde timestamps | nieuwste timestamp als `as_of` |
| Issueprojectie | exact één concrete actie |
