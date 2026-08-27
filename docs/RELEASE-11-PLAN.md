# Release 11 plan — daadwerkelijke domeinextractie

## Doel

Release 11 voert de structurele extracties uit die na Release 10 nog niet
zijn gerealiseerd. De read API en sync-orchestrator worden teruggebracht tot
facades/coördinatoren; domeinlogica verhuist naar zelfstandig testbare
componenten. De bestaande query-budgetten en SQL statement counter worden
gebruikt als harde regressiegates.

De publieke API, OpenAPI-responses, tenant-scoping, idempotentie, outbox-
semantiek, cursors en UnitOfWork-grenzen blijven ongewijzigd.

## Uitgangspositie

- `services/read/accounts.py`, `prices.py`, `budgets.py`, `benchmarks.py` en
  `query_counter.py` bestaan.
- `read_api.py` bevat nog circa 2.150 regels en is nog eigenaar van portfolio,
  holdings, securities en analytics-SQL.
- `sync/persistence.py` is nog een forwarding adapter; concrete upserts,
  change detection en outbox-operaties staan nog in `orchestrator.py`.
- De lokale niet-integration/non-E2E suite is groen.
- PostgreSQL/Redis integration, migration en E2E-validatie moeten via Docker
  of CI worden uitgevoerd.
- Pyright staat op 69 warnings; nieuwe code moet warning-vrij blijven.

## Prioriteiten

| Prioriteit | Werkpakket | Definition of done |
|---|---|---|
| Kritiek | Read-componenten | Portfolio, holdings, securities en analytics hebben zelfstandige services; facade bevat geen domein-SQL. |
| Kritiek | Concrete persistence | Upsert, change detection, security resolution en outbox zitten achter een echte persistence-implementatie. |
| Kritiek | Contractbehoud | OpenAPI en characterization tests blijven groen zonder responsewijzigingen. |
| Gemiddeld | Performance gates | Echte SQLAlchemy-sessies gebruiken budgets en benchmarkprofielen. |
| Gemiddeld | Typekwaliteit | Baseline daalt minimaal naar 60 warnings. |
| Laag | Productiegates | PostgreSQL/Redis/E2E, scans, staging en rollback zijn aantoonbaar uitgevoerd. |

## Uitvoeringsfases

### 11.1 Contract- en facade-lock

1. Voeg characterization tests toe voor portfolio, holdings, securities,
   prices, history, net-worth en cashflow.
2. Leg response snapshots en OpenAPI-baseline vast.
3. Definieer de serviceinterfaces met `AsyncSession`, `ReadScope` en expliciete
   filters/pagination; services mogen geen facade-internals importeren.
4. Breid de query-count fixture uit zodat elk component aan een named budget
   uit `READ_QUERY_BUDGETS` kan worden gekoppeld.

Acceptatie: de tests falen bij response-, scope- of querybudgetregressies.

### 11.2 Portfolio- en holdingsservice

1. Maak `services/read/portfolio.py` voor portfolio, holdings, balances,
   valuation, freshness en coverage.
2. Verplaats de bestaande SQL en mappinglogica zonder semantische wijziging.
3. Laat `ReadService.get_portfolio`, `get_holdings` en gerelateerde routes
   uitsluitend delegeren.
4. Dek empty results, stale/unpriced prices, `as_of`, account-/tenant-scope
   en currency metadata af.

Acceptatie: geen portfolio-/holdings-SQL meer in `read_api.py`; component-
tests en characterization tests zijn groen.

### 11.3 Securities- en analyticsservices

Maak:

- `services/read/securities.py` voor securities, listings en prices;
- `services/read/analytics.py` voor portfolio history, net-worth en cashflow.

Gebruik de bestaande pagination-, freshness- en latest-price-helpers. Test
search/filter/sort, datumgrenzen, missing prices, currency conversion,
pagination en tenant-scope.

Acceptatie: alle betrokken routes delegeren; `read_api.py` bevat maximaal 300
regels facade-/compatibiliteitscode en geen domein-SQL.

### 11.4 Concrete sync-persistence

1. Maak een concrete `SyncPersistence`-implementatie met expliciete tenant-,
   provider- en connection-context.
2. Verplaats account-, transaction- en holding-upsert, change detection,
   revision handling en outbox create/update.
3. Maak security resolution een afzonderlijke `SecurityResolver` dependency
   voor transaction- en holdings-persistence.
4. Laat de stages alleen de persistence-interface gebruiken; commits en
   rollbacks blijven bij de caller-owned UnitOfWork.
5. Verwijder `_upsert_*` en `_resolve_security_reference` uit de orchestrator
   nadat bestaande tests naar de nieuwe component verwijzen.

Acceptatie: create, changed update, unchanged update, duplicate sync,
unresolved security, outbox-idempotentie en rollback zijn afzonderlijk getest;
de orchestrator bevat alleen coördinatie.

### 11.5 Echte query- en benchmarkgates

1. Voer portfolio-, holdings-, securities- en analyticsqueries uit tegen
   PostgreSQL met de `QueryCounter`.
2. Gebruik de deterministische profielen `holdings-100` en `holdings-1000`.
3. Publiceer query count en latency als CI-artifacts met dataset- en
   databaseversie.
4. Voeg een negatieve test toe die bewust N+1-gedrag introduceert en moet
   falen.

Acceptatie: de actuele componenten blijven binnen hun named budgets en de
latest-price query blijft één batch statement.

### 11.6 Real-service en release-readiness

1. Draai PostgreSQL 16 migrations: `upgrade head → downgrade base → upgrade
   head`.
2. Draai Redis 7-tests voor locks, webhook throttling en outbox/workflows.
3. Draai API → worker → outbox E2E en maak onverwachte skips CI-fouten.
4. Voer Ruff, Pyright, pip-audit, CycloneDX en Trivy uit.
5. Controleer OpenAPI diff, lockfile, image build en staging smoke met
   synthetische data.
6. Werk README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook bij.

Acceptatie: alle artifacts zijn beschikbaar; ontbrekende Docker-lokale
validatie is vervangen door een geslaagde CI-run, niet door een skip.

## Volgorde en afhankelijkheden

```text
11.1 → 11.2 → 11.3 → 11.4 → 11.5 → 11.6
  └──────────── query-fixtures en OpenAPI snapshots parallel ────────────┘
```

11.1 is een harde poort vóór het verwijderen van legacycode. 11.2 en 11.3
delen contracten maar kunnen per domein worden uitgevoerd. 11.4 volgt na de
stage-characterization tests. 11.6 is alleen groen wanneer alle extracties en
performance-gates klaar zijn.

## Buiten scope

- Geen nieuwe endpoints, responsevelden of publieke API-contracten.
- Geen nieuwe connectors, exporters of providerintegraties.
- Geen microservice-extractie.
- Geen productiedowngrade als rollbackstrategie.
- Geen echte financiële data in fixtures of staging.

## Release-acceptatie

Release 11 is gereed wanneer:

1. `read_api.py` maximaal 300 regels facade-/compatibiliteitscode bevat;
2. `orchestrator.py` geen concrete entity-persistence meer bevat;
3. alle read- en write-componenten zelfstandig getest zijn;
4. querybudgetten en benchmarks tegen echte DB-sessies slagen;
5. migrations, PostgreSQL, Redis en E2E in CI groen zijn;
6. Pyright maximaal 60 warnings rapporteert;
7. security-, OpenAPI-, staging- en rollbackbewijs is vastgelegd.

