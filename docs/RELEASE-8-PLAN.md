# Release 8 plan — afronding van de modularisering

## Doel

Release 8 sluit de resterende Release 7-werkzaamheden af. De read-kant wordt
opgesplitst in volledige domeincomponenten, persistence wordt een expliciete
sync-boundary en de performance- en real-service gates worden uitgevoerd.

Er komen geen nieuwe endpoints, connectoren of exporters. API-responses,
tenant-scoping, idempotentie, cursors en UnitOfWork-grenzen blijven leidend.

## Openstaande punten

- `read_api.py` bevat nog circa 2.150 regels legacy-query- en mappinglogica.
- `sync/orchestrator.py` bevat nog circa 2.100 regels coördinatie en entity-
  persistence; account-, transaction- en holdings-stages bestaan inmiddels.
- Portfolio-, securities- en analytics-readservices zijn nog niet volledig
  geïsoleerd.
- Gedeelde `sync/persistence.py` ontbreekt.
- Endpoint query-budgets, benchmarkdatasets en latency-baselines ontbreken.
- PostgreSQL/Redis integration-, migration- en E2E-gates zijn lokaal niet
  uitgevoerd door het ontbreken van Docker.
- Pyright staat nog op de baseline van 69 warnings.

## Prioriteiten

| Prioriteit | Onderwerp | Definition of done |
|---|---|---|
| Kritiek | Read-domeinen | Portfolio, securities en analytics zijn zelfstandige services; facade bevat geen SQL. |
| Kritiek | Persistence-boundary | Orchestrator coördineert stages en transacties, maar bevat geen upsertdetails. |
| Kritiek | Real-service verificatie | Migration, integration en E2E slagen in CI zonder onverwachte skips. |
| Gemiddeld | Performance-gates | Query-budgetten en 100/1.000-holdingsbenchmarks zijn reproduceerbaar. |
| Gemiddeld | Type debt | Warning-baseline daalt onder 69; nieuwe componenten zijn warning-vrij. |
| Laag | Release hygiene | OpenAPI, scans, documentatie, staging en rollback zijn gecontroleerd. |

## Uitvoeringsfases

### 8.1 Portfolio-readservice

1. Maak `services/read/portfolio.py` eigenaar van portfolio, holdings en
   balances.
2. Verplaats DTO-mapping, freshness/coverage metadata en scopefilters zonder
   contractwijziging.
3. Laat portfolio- en holdingsroutes rechtstreeks via de component lopen.
4. Verwijder de gedupliceerde implementatie uit `read_api.py` nadat de
   characterization tests groen zijn.

Acceptatie: componenttests dekken tenant-/account-scope, stale/unpriced
holdings, valuta en portfolio-meta; de facade bevat geen portfolio-SQL meer.

### 8.2 Securities- en analytics-readservices

Extraheer:

- `services/read/securities.py` voor securities, listings en prices;
- `services/read/analytics.py` voor history, net-worth en cashflow;
- gedeelde response-contracten voor as-of/freshness/coverage.

Gebruik `pagination.py` en de set-based `prices.py` helper. Elke component
ontvangt alleen `AsyncSession` en `ReadScope` en is onafhankelijk testbaar.

Acceptatie: OpenAPI blijft compatibel, routes gebruiken de componenten en
`read_api.py` is maximaal 300 regels facade-/compatibiliteitscode.

### 8.3 Persistence-boundary

1. Voeg `sync/persistence.py` toe voor account-, transaction- en holding-
   upsert/change-detection/outbox-operaties.
2. Laat de bestaande stages via deze boundary schrijven.
3. Houd security resolution als expliciete dependency van de transaction- en
   holdings-stages.
4. Behoud één UnitOfWork per pipeline en verbied autonome commits in stages.

Acceptatie: persistence-tests dekken create, update, unchanged, outbox,
rollback en duplicate sync; `orchestrator.py` bevat geen entity-upsertdetails.

### 8.4 Query-budgetten en benchmarks

- Voeg een SQLAlchemy query-count fixture toe voor portfolio, securities,
  net-worth en cashflow.
- Stel per endpoint een budget vast; latest prices blijven één batch-query.
- Voeg deterministische datasets toe voor 100 en 1.000 holdings en meerdere
  sync-accounts.
- Meet query-aantal en latency met vastgelegde hardware-/dataset-aannames en
  bewaar de baseline als CI-artifact.

Acceptatie: kunstmatige N+1- of latency-regressies laten de gate falen.

### 8.5 Real-service gates

1. Draai migrations op een lege PostgreSQL-database en voer de roundtrip
   `upgrade head → downgrade base → upgrade head` uit.
2. Draai integrationtests tegen PostgreSQL 16 en Redis 7 voor locks, webhook
   throttling, outbox, sync-idempotentie en index `0037`.
3. Draai de volledige API → worker → outbox E2E-suite zonder onverwachte
   skips.
4. Documenteer Docker Compose als lokale route; CI is de formele route als
   Docker lokaal ontbreekt.

### 8.6 Type debt en release-readiness

- verlaag de Pyright-baseline onder 69 en houd nieuwe modules warning-vrij;
- voer pip-audit, CycloneDX en Trivy uit;
- controleer OpenAPI diff, Alembic chain en lockfile-consistentie;
- werk README, ARCHITECTURE, DATABASE, UPGRADE en rollback-runbook bij;
- voer staging smoke tests uit vóór promotion.

## Volgorde

```text
8.1 → 8.2 → 8.3 → 8.4 → 8.5 → 8.6
```

Het testharnas en benchmarkfixtures kunnen parallel worden voorbereid tijdens
8.1–8.3. De eindgates draaien pas nadat de componentextracties klaar zijn.

## Buiten scope

- Geen nieuwe publieke API-contracten of productfeatures.
- Geen nieuwe connectoren/exporters.
- Geen microservice-extractie.
- Geen destructieve productiedowngrade als rollbackstrategie.

## Release-acceptatie

Release 8 is gereed wanneer de read-facade en sync-orchestrator alleen nog
delegatie/coördinatie bevatten, persistence en alle domeincomponenten apart
getest zijn, unit/integration/migration/E2E groen zijn, query-/benchmarkgates
slagen, de Pyright-baseline daalt en alle security-, OpenAPI-, staging- en
rollbackchecks afgerond zijn.
