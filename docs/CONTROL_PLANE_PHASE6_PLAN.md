# Implementatieplan fase 6 — analytische consumentenlaag

## Doel

Maak de analytische functies een consistente consumentenlaag van de canonieke
dataset. Portfolio, performance, benchmarks, cashflow, subscriptions,
marktintelligentie en AI-samenvattingen mogen geen eigen operationeel
statusmodel introduceren en moeten hun herkomst, actualiteit en dekking
expliciet teruggeven.

## Huidige situatie

- Portfolio en performance hebben al read-services en gedeeltelijk een
  `AggregateMeta`/freshness-contract.
- Cashflow en subscriptions leveren inhoudelijke resultaten, maar niet
  hetzelfde metadata-envelope.
- Marktintelligentie en AI hebben bestaande endpoints/services, maar worden
  nog niet als één analytische read-projectie geconsumeerd.
- De control plane blijft de bron voor operationele problemen; analytische
  responses mogen geen tweede issue-/runstatus introduceren.

## Scope

1. **Analytics-contract**
   - Voeg een typed `AnalyticsOverview`-contract toe met optionele secties
     voor portfolio, performance, cashflow, subscriptions,
     marktintelligentie en AI.
   - Voeg één metadata-envelope toe met `as_of`, freshness (`fresh`,
     `stale`, `partial`, `unavailable`), coverage en korte caveats.
   - Een lege of ontbrekende bron levert een geldige optionele sectie op en
     geen 500.

2. **Composer-service**
   - Voeg een tenant-scoped `AnalyticsOverviewService` toe.
   - Composeer bestaande read-services; dupliceer geen SQL of domeinregels.
   - Respecteer de bestaande `ReadScope` voor account-zichtbaarheid.
   - Gebruik alleen canonieke `Account`, `Holding`, `Transaction`,
     `SecurityPrice` en bestaande enrichment/intelligence-opslag.

3. **API**
   - Voeg een read-only `GET /api/v1/analytics/overview` endpoint toe met
     bestaande auth- en permissiepatronen.
   - Ondersteun `date_from`, `date_to` en optioneel een benchmark-id.
   - Houd bestaande afzonderlijke analytics-endpoints backwards compatible.

4. **Validatie en documentatie**
   - Test tenant-isolatie, account-scope, lege datasets, stale/partial
     metadata, veilige AI-input en deterministische response-shapes.
   - Voeg de endpoint toe aan OpenAPI en documenteer dat freshness/coverage
     caveats de interpretatie van cijfers begrenzen.

## Buiten scope

- Nieuwe connectors, nieuwe marktdata-aanbieders of nieuwe modellen.
- Een persistente analytics- of control-plane-issue-tabel.
- Nieuwe AI-prompts/providerintegraties of portfolio-algoritmes.
- Wijzigingen aan de operationele control-plane statusfeed.

## Uitvoeringsvolgorde

1. Contract en metadata-adapters voor bestaande analytics-responses.
2. Composer-service met scope- en tenantgrenzen.
3. Endpoint, routerregistratie en OpenAPI.
4. Unit/API-contracttests en regressietests voor bestaande endpoints.
5. Ruff, Pyright, volledige unit-CI met coverage en beschikbare
   PostgreSQL/Redis-gates.

## Acceptatiecriteria

- `GET /api/v1/analytics/overview` retourneert een typed, tenant-scoped
  contract voor een lege en gevulde dataset.
- Iedere aanwezige analytische sectie bevat `as_of`, freshness, coverage en
  eventuele caveats.
- Geen analytische query leest data van een andere tenant of buiten de
  bestaande account-scope.
- Bestaande analytics-endpoints en control-plane endpoints blijven groen.
- Ruff, format, Pyright, tests en coverage-gate zijn groen.
