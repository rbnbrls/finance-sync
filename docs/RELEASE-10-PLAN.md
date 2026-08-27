# Release 10 plan — modulaire read/write-kern en productiegates

## Doel

Release 10 realiseert de structurele punten die na Release 9 nog openstaan:
de read-facade en sync-orchestrator worden dunne coördinatielagen, concrete
persistence verhuist naar een zelfstandige component en de bestaande query-
budgetten worden aan echte database-tests gekoppeld.

De release wijzigt geen publieke endpoints, response-schema's, tenant-
scoping, idempotentie, cursors, outbox-semantiek of UnitOfWork-contracten.
Nieuwe connectoren, exporters en productfeatures vallen buiten scope.

## Uitgangspositie

- Query-budgetcontracten bestaan in `services/read/budgets.py`.
- `services/read/accounts.py` en `services/read/prices.py` zijn bestaande
  componenten en blijven referentie-implementaties.
- `read_api.py` bevat nog circa 2.150 regels, inclusief legacy portfolio-,
  securities- en analyticslogica.
- `sync/orchestrator.py` bevat nog circa 2.100 regels, inclusief concrete
  upsert-, change-detection- en outboxlogica.
- `sync/persistence.py` bestaat als adapter, maar is nog geen eigenaar van de
  concrete persistence-operaties.
- De lokale unit/regressiesuite is groen; PostgreSQL-, Redis- en E2E-tests
  zijn afhankelijk van Docker/CI.
- De Pyright-warningbaseline is 69.

## Prioriteiten

| Prioriteit | Werkpakket | Definition of done |
|---|---|---|
| Kritiek | Read decompositie | Portfolio, securities en analytics zijn zelfstandige services; facade bevat geen domein-SQL. |
| Kritiek | Write decompositie | `SyncPersistence` bezit upserts, change detection, security resolution en outbox-aanroepen; orchestrator coördineert alleen. |
| Kritiek | Real-service CI | Migration, PostgreSQL 16, Redis 7 en API-worker-outbox E2E-gates zijn actief en falen bij onverwachte skips. |
| Gemiddeld | Performance | Querybudgetten worden tegen echte SQLAlchemy-sessies gemeten met deterministische datasets. |
| Gemiddeld | Typekwaliteit | Warningbaseline daalt aantoonbaar onder 69. |
| Laag | Release hygiene | OpenAPI diff, dependency/image scans, staging smoke en rollbackrunbook zijn reproduceerbaar. |

## Uitvoeringsfases

### 10.1 Contract-lock en testharnas

1. Voeg characterization tests toe voor alle read-endpoints die worden
   verplaatst: portfolio, holdings, securities, prices, history, net-worth en
   cashflow.
2. Maak gedeelde read-contracten voor `ReadScope`, pagination, as-of,
   freshness en coverage zonder imports vanuit componenten naar de facade.
3. Maak een query-count fixture die SQLAlchemy statements per test verzamelt
   en koppelt aan `READ_QUERY_BUDGETS`.
4. Leg OpenAPI en response snapshots vast vóór de extracties.

Acceptatie: de testharnas detecteert contractwijzigingen en een querybudget-
overschrijding faalt deterministisch.

### 10.2 Portfolio- en holdingscomponent

1. Maak `services/read/portfolio.py` eigenaar van portfolio, holdings,
   balances, valuation en freshness metadata.
2. Injecteer alleen `AsyncSession` en `ReadScope`; de component importeert
   geen DTO's of helpers uit `read_api.py` die querygedrag bevatten.
3. Laat de facade alleen delegeren en verwijder de oude SQL-/mappingblokken.
4. Behoud exact gedrag voor tenant/account-scope, valuta, stale prices,
   unpriced holdings, empty results en `as_of`.

Acceptatie: portfolio- en holdingscomponenten zijn zelfstandig testbaar en
`read_api.py` bevat geen portfolio-/holdings-SQL meer.

### 10.3 Securities- en analyticscomponenten

Extraheer:

- `services/read/securities.py` voor securities, listings en prices;
- `services/read/analytics.py` voor portfolio history, net-worth en cashflow;
- gedeelde response-contracten voor metadata en pagination.

Gebruik de bestaande set-based latest-price-helper en pagination-helper. Test
filtering, sorting, missing prices, currency conversion, datumgrenzen,
tenant-scope en pagination.

Acceptatie: routes delegeren naar componenten en `read_api.py` is maximaal
300 regels facade-/compatibiliteitscode.

### 10.4 Concrete sync-persistence

1. Maak `SyncPersistence` verantwoordelijk voor account-, transaction- en
   holding-upsert.
2. Verplaats change detection, revision-updates en outbox create/update naar
   die component.
3. Maak security resolution een expliciete `SecurityResolver` dependency van
   transaction- en holdings-persistence.
4. Geef tenant-, provider- en connection-context expliciet mee in de
   interfaces.
5. Laat de orchestrator één persistence-instance maken en uitsluitend stages,
   cursors en run-status coördineren.
6. Verwijder de private `_upsert_*`- en `_resolve_*`-implementaties uit de
   orchestrator nadat characterization tests groen zijn.

Acceptatie: persistence-tests dekken create, changed/unchanged update,
duplicate sync, outbox-idempotentie, unresolved security en rollback. Geen
stage of persistence-component voert autonoom commit/rollback uit.

### 10.5 Database benchmarks en real-service gates

1. Voeg deterministische PostgreSQL-fixtures toe voor 100 en 1.000 holdings,
   meerdere accounts en ontbrekende/stale prices.
2. Meet query-aantal per read-component tegen de vastgelegde budgets.
3. Bewaar latency-baselines als CI-artifact met dataset- en hardwaremetadata;
   gebruik latency niet als enige correctness-gate.
4. Voer migration `upgrade head → downgrade base → upgrade head` uit op een
   lege PostgreSQL 16-database.
5. Test Redis 7 voor locks, webhook rate limiting en outbox/workflowgedrag.
6. Voer API → worker → outbox E2E uit en maak onverwachte skips fouten.

Acceptatie: querybudgetten slagen op echte DB-sessies en alle service-gates
produceren herleidbare logs/artifacts.

### 10.6 Type-, security- en release-readiness

- verlaag Pyright van 69 naar maximaal 60 warnings;
- houd nieuwe componenten warning-vrij;
- voer Ruff, pip-audit, CycloneDX en Trivy uit;
- controleer OpenAPI diff, Alembic chain, lockfile en image build;
- werk README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook bij;
- voer staging smoke uit met uitsluitend synthetische financiële data.

Acceptatie: alle checks zijn geautomatiseerd of hebben een vastgelegd
reproduceerbaar bewijsstuk.

## Volgorde en afhankelijkheden

```text
10.1 → 10.2 → 10.3 → 10.4 → 10.5 → 10.6
  └────────────── query-fixtures en CI-harnas parallel ──────────────┘
```

10.1 is een harde poort vóór het verwijderen van legacylogica. 10.2 en 10.3
kunnen per domein worden uitgevoerd, maar delen de contracten uit 10.1.
10.4 volgt na stabiele stage-contracten. 10.5 en 10.6 zijn eindgates en mogen
geen openstaande extractie verbergen.

## Buiten scope

- Geen nieuwe publieke endpoints of API-contracten.
- Geen nieuwe connectoren, exporters of providerintegraties.
- Geen microservice-extractie.
- Geen productie-downgrade als rollbackstrategie.
- Geen echte financiële data in fixtures of staging.

## Release-acceptatie

Release 10 is gereed wanneer:

1. `read_api.py` alleen facade-/delegatielogica bevat;
2. `sync/orchestrator.py` alleen pipelinecoördinatie bevat;
3. `SyncPersistence` concrete write- en outboxlogica bezit;
4. echte querybudgetten en deterministische benchmarks slagen;
5. migration-, PostgreSQL-, Redis- en E2E-gates groen zijn;
6. Pyright maximaal 60 warnings rapporteert;
7. security-, OpenAPI-, staging- en rollbackbewijs is vastgelegd.

