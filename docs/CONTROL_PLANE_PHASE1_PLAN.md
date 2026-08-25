# Implementatieplan fase 1 — control-plane contract en backendaggregatie

## Doel

Exposeer één tenant-scoped, read-only contract op
`GET /api/v1/control-plane/overview`. De endpoint projecteert bestaande
bron-, sync-, kwaliteits- en bestemmingsstatussen naar één stabiel API-model,
zonder een persistente `control_plane_issues`-tabel te introduceren.

## Scope

1. `src/finance_sync/schemas/control_plane.py`
   - Pydantic-contract voor metadata, samenvatting, verbindingen, syncs,
     issues, freshness, brondekking en bestemmingen.
   - Een issue bevat altijd een stabiele `id`, severity, categorie, uitleg en
     precies één concrete read-only vervolgstap.
2. `src/finance_sync/services/control_plane.py`
   - Tenant-scoped read service met één `get_overview()` ingang.
   - Parallelle aggregatie uit bestaande tabellen.
   - Deterministische statusregels: `healthy`, `attention_required`,
     `sync_failed`, `partial`.
   - Freshness-classificatie met een configureerbare/default grens van 24 uur.
   - Geen secrets, stack traces of ruwe databasefouten in het contract.
3. `src/finance_sync/api/v1/control_plane.py`
   - `GET /control-plane/overview` met bestaande `control-plane:read`
     permissie.
   - Routerregistratie in `api/v1/router.py`.
4. Tests
   - Schema-validatie en status/severityregels met fake/SQLAlchemy-sessies.
   - API-contract, authenticatie/permissie en tenant-isolatie waar de huidige
     modellen dat kunnen afdwingen.

## Aggregatiebeslissingen

- Verbindingen komen uit `Credential`; labels worden uit het bestaande
  `description`-JSON gelezen zonder secrets te ontsleutelen.
- Syncs worden per verbinding uit `SyncRun` gehaald. Legacy runs zonder
  `connection_id` worden niet aan een tenant toegeschreven.
- Open unresolved-security issues worden alleen gekoppeld aan providers van
  de tenant. Omdat de legacy-tabel geen `tenant_id` bevat, blijft dit een
  compatibiliteitsprojectie; een latere migratie kan dit afdwingen.
- Freshness wordt berekend voor securities die via holdings van de tenant
  bereikbaar zijn; securities zonder holdings worden niet globaal meegeteld.
- Destinations komen uit `ExportTarget`; health- en configuratiestatus worden
  geprojecteerd. Legacy `ExportRun` heeft geen tenant/target-relatie, dus
  exportstatus wordt alleen toegevoegd zodra een betrouwbare tenantrelatie
  beschikbaar is.
- `as_of` is de nieuwste bron-timestamp die in de projectie is gebruikt;
  `generated_at` is het moment waarop de overview is opgebouwd.

## Verificatie en acceptatie

- Lege database retourneert een geldig, leeg contract met `status=healthy`.
- Eén mislukte tenant-sync maakt `sync_failed` en levert één retry/view issue.
- Eén unresolved security levert één `security_mapping` issue met link naar
  `/api/v1/securities/unresolved`.
- Stale of ontbrekende quotes worden als freshness-issue weergegeven.
- Records van een andere tenant verschijnen niet in connections, syncs,
  freshness, destinations of issue counts.
- `pytest` voor de nieuwe tests, daarna Ruff en pyright op gewijzigde code.

## Buiten scope van fase 1

Retry-endpoints, sync-run detail, security mapping-mutaties, export recovery,
uniforme foutcategorieën, GUI en schema-migraties voor tenantless legacy
tabellen. Deze volgen in latere fasen.

## Uitvoeringsvolgorde

1. Contract en pure classificatie/helpers.
2. Tenant-scoped SQL-aggregatieservice.
3. API-router en registratie.
4. Tests, type/lintcontrole en endpoint smoke test.
