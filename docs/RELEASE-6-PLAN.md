# Release 6 plan — afronding van modulariteit en release-gates

## Doel

Release 6 rondt de resterende Release 5-werkzaamheden af. De account-stage is
al geëxtraheerd; deze release verplaatst de overige read- en sync-logica naar
duidelijke componenten en maakt de echte service-verificatie reproduceerbaar.

Er komen geen nieuwe publieke endpoints, connectoren of exporters. Bestaande
response-contracten, tenant-scoping, idempotentie, cursors en UnitOfWork-
grenzen blijven ongewijzigd.

## Openstaande punten

- `services/read_api.py` bevat nog circa 2.100 regels query- en mappinglogica.
- `sync/orchestrator.py` bevat nog circa 2.000 regels transacties, holdings,
  persistence en pipeline-coördinatie.
- Alleen de accountfase is als aparte sync-stage geëxtraheerd.
- Query-budgettests zijn beperkt tot de latest-price helper; endpoint-budgets
  en 100/1.000-holdingsbenchmarks ontbreken.
- PostgreSQL/Redis integration- en E2E-tests zijn lokaal niet uitvoerbaar
  zonder Docker; CI moet de formele gate blijven.
- De Pyright-warningbaseline staat nog op 69.

## Prioriteiten

| Prioriteit | Onderwerp | Definition of done |
|---|---|---|
| Kritiek | Read API | Facade + accounts, portfolio, securities en analytics services; facade bevat geen SQL. |
| Kritiek | Sync pipeline | Transactions, holdings en persistence zijn typed stages naast accounts. |
| Kritiek | Real-service gates | Integration, migration en E2E draaien volledig groen in CI. |
| Gemiddeld | Performance | Endpoint query-budgets en reproduceerbare benchmarks falen bij regressie. |
| Gemiddeld | Type debt | Warning-count daalt onder 69; nieuwe modules blijven warning-vrij. |
| Laag | Release hygiene | Docs, dependency scans, OpenAPI en rollback-runbook zijn compleet. |

## Uitvoeringsfases

### 6.1 Read-service: accounts en transacties

1. Verplaats account- en transactielijsten naar
   `services/read/accounts.py`.
2. Houd response-DTO's in een stabiele contracts-module en laat
   `services/read/facade.py` alleen scope/context en delegatie verzorgen.
3. Voeg characterization tests toe voor tenant/account-scope, sortering,
   paginatie, filters en foutresponses.

Acceptatie: `read_api.py` bevat deze SQL niet meer; bestaande endpointtests en
OpenAPI diff blijven groen; query-counts zijn niet hoger dan de baseline.

### 6.2 Read-service: portfolio, securities en analytics

Maak `portfolio.py`, `securities.py` en `analytics.py` leidend voor
respectievelijk holdings/balances, securities/prices en historie/net-worth/
cashflow. Hergebruik `pagination.py` en `prices.py`; duplicate mappings en
N+1-queries worden verwijderd.

Acceptatie: de facade is maximaal 300 regels, alle components zijn afzonderlijk
testbaar en elke component ontvangt uitsluitend `AsyncSession` en `ReadScope`.

### 6.3 Sync pipeline: transacties, holdings en persistence

1. Breid `SyncContext` uit met run-id, cursor en structured logger-context.
2. Extraheer `transactions.py` voor transacties, cards en scheduled payments.
3. Extraheer `holdings.py` voor security resolution en holdings.
4. Extraheer `persistence.py` voor upsert, change detection en outbox.
5. Laat `SyncOrchestrator` alleen stagevolgorde, UnitOfWork, rollback, cursor-
   commit en resultaataggregatie beheren.

Geen stage voert zelfstandig `commit()` uit. Elke stage krijgt success-,
transient-, permanent-, rollback- en idempotentietests.

Acceptatie: `orchestrator.py` is maximaal 300 regels; bestaande sync- en
outbox-semantiek blijft gelijk.

### 6.4 Query budgets en benchmarks

- Voeg een query-count fixture toe voor portfolio, securities, net-worth en
  cashflow endpoints.
- Stel budgets vast op basis van characterization tests; latest prices blijven
  één batch-query.
- Voeg benchmarkfixtures toe voor 100 en 1.000 holdings en syncs met meerdere
  accounts.
- Leg hardware, dataset en toegestane latency-/query-afwijking vast.

Acceptatie: een N+1- of latency-regressie boven de afgesproken drempel faalt
de performance-gate.

### 6.5 PostgreSQL/Redis en E2E-verificatie

1. Laat CI de bestaande PostgreSQL 16- en Redis 7-servicecontainers starten.
2. Valideer migratie `upgrade head → downgrade base → upgrade head`, inclusief
   index `0037`.
3. Valideer Redis-webhook throttling, locks, outbox en sync-idempotentie.
4. Laat de E2E API → worker → outbox-suite zonder onverwachte skips slagen.
5. Documenteer Docker Compose als lokale reproduceerroute; zonder Docker is de
   CI-run de formele verificatie.

### 6.6 Type debt en release-readiness

- verlaag de Pyright-baseline stapsgewijs vanaf 69;
- verwijder warnings in alle gewijzigde Release 6-modules;
- voer `pip-audit`, CycloneDX en Trivy uit;
- controleer lockfile/dependencies en OpenAPI diff;
- werk ARCHITECTURE, DATABASE, README, UPGRADE en rollback-runbook bij;
- voer staging smoke tests uit vóór promotion.

## Volgorde

```text
6.1 → 6.2 → 6.3 → 6.4 → 6.5 → 6.6
```

Het CI-testharnas van 6.5 kan parallel worden voorbereid met 6.1–6.3, maar de
finale gates draaien pas na de service-extracties.

## Buiten scope

- Geen microservice-extractie of deployment-splitsing.
- Geen publieke contractwijzigingen.
- Geen nieuwe productfunctionaliteit.
- Geen destructieve productiedowngrade als rollbackstrategie.

## Release-acceptatie

Release 6 is gereed wanneer `read_api.py` en `orchestrator.py` alleen nog
facade/coördinatie bevatten, alle stages afzonderlijk getest zijn, unit- en
real-service suites groen zijn, query-/benchmarkgates slagen, de Pyright-
baseline daalt en security-, OpenAPI-, staging- en rollbackchecks afgerond
zijn.
