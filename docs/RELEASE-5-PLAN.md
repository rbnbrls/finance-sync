# Release 5 plan — service-extractie en volledige verificatie

## Doel

Release 5 sluit de resterende technische schuld uit Release 4 af. De twee
grote legacy-services worden daadwerkelijk opgesplitst, waarna de
PostgreSQL/Redis-omgeving, query-performance en typekwaliteit als harde
releasegates worden gevalideerd.

Geen nieuwe connectoren, endpoints of productfeatures vallen binnen deze
release. Bestaande API-responses, tenant-scoping, idempotentie en
transactionele grenzen blijven leidend.

## Openstaande punten

- `services/read_api.py` is nog circa 2.100 regels.
- `sync/orchestrator.py` is nog circa 2.000 regels.
- De lokale omgeving heeft geen Docker; integration-, migration- en E2E-gates
  zijn daarom nog niet lokaal bewezen.
- Query-budgettests en benchmark-baselines ontbreken.
- De Pyright-baseline staat op 69 warnings en moet dalen.

## Prioriteiten

| Prioriteit | Onderwerp | Resultaat |
|---|---|---|
| Kritiek | Read-service extractie | `ReadService` wordt een facade; domeinen krijgen eigen services. |
| Kritiek | Sync-stage extractie | Orchestrator coördineert typed stages zonder persistence-details. |
| Kritiek | Real-service verificatie | PostgreSQL, Redis, migraties en E2E draaien aantoonbaar groen in CI. |
| Gemiddeld | Query budgets/benchmarks | N+1-regressies en schaalproblemen falen automatisch. |
| Gemiddeld | Type debt | Warning-count daalt; nieuwe modules zijn warning-vrij. |
| Laag | Release hygiene | Dependency-, image-, documentatie- en rollback-controles zijn compleet. |

## Fases

### 5.1 Baseline en characterization

1. Leg response-shapes, foutcodes, scopes, sortering en paginatie vast in
   characterization tests.
2. Voeg query-count instrumentation toe voor portfolio, securities, net-worth
   en cashflow.
3. Definieer benchmarkdatasets voor 100 en 1.000 holdings en meerdere
   connectoraccounts.
4. Voeg de huidige Pyright-warninglijst toe aan de Release 5-baseline en
   markeer per warning eigenaar/module.

Acceptatie: de baseline is reproduceerbaar en detecteert API-, query- en
type-regressies vóór refactoring.

### 5.2 Read-service extractie

Maak deze modules leidend:

- `services/read/facade.py` — scope/context en compatibiliteitsinterface;
- `services/read/accounts.py` — accounts en transacties;
- `services/read/portfolio.py` — holdings, balances en portfolio;
- `services/read/securities.py` — securities, listings en prijzen;
- `services/read/analytics.py` — historie, net-worth en cashflow.

Verplaats de bestaande methodes zonder DTO-wijziging. Elke component krijgt
alleen een getypeerde `AsyncSession` en `ReadScope`. De facade mag geen
SQLAlchemy-query meer bevatten.

Acceptatie:

- `read_api.py` bevat geen domeinqueries meer en is maximaal 300 regels;
- alle bestaande read-, endpoint- en OpenAPI-contracttests slagen;
- scoping wordt per component getest;
- query-counts zijn gelijk of lager dan de baseline.

### 5.3 Sync-stage extractie

Introduceer een immutable `SyncContext` en stages voor:

- authenticatie en connector-state;
- accounts;
- transacties, cards en scheduled payments;
- holdings en security resolution;
- persistence/change detection/outbox.

De orchestrator beheert uitsluitend stagevolgorde, UnitOfWork, cursor-commit,
rollback en resultaataggregatie. Stages committen nooit zelfstandig.

Acceptatie:

- iedere stage heeft success-, transient-, permanent- en rollback-tests;
- duplicate syncs blijven idempotent;
- outbox-events blijven transactioneel;
- `orchestrator.py` is maximaal 300 regels;
- bestaande integrationtests blijven semantisch gelijk.

### 5.4 Integration-, migration- en E2E-verificatie

1. Gebruik de bestaande CI-servicecontainers voor PostgreSQL 16 en Redis 7.
2. Laat `pytest -m integration` en `pytest -m e2e` verplicht slagen, niet
   alleen collectioneren of skippen.
3. Valideer `upgrade head → downgrade base → upgrade head` op een verse
   database.
4. Voeg assertions toe voor migratie `0037`, latest-price index,
   Redis-webhook throttling en outbox/idempotentie.
5. Documenteer lokaal uitvoeren met Docker Compose; wanneer Docker lokaal
   ontbreekt, is de CI-run de formele verificatie.

Acceptatie: een schone CI-run rapporteert nul onverwachte skips in de
integration- en E2E-jobs en alle roundtrips slagen.

### 5.5 Query budgets, benchmarks en type debt

- Stel per endpoint query-budgetten vast, met maximaal één latest-price query
  per batch en expliciete N+1-detectie.
- Run de 100/1.000-holdingsbenchmarks in CI of een dedicated performance job;
  leg hardware, dataset en drempels vast.
- Verwijder Pyright-warnings in gewijzigde modules eerst en verlaag daarna de
  globale budgetwaarde stapsgewijs van 69 naar 0.
- Voeg typed protocols toe voor stage-contexten, session factories en Redis.

Acceptatie: budgets en benchmarks falen bij regressie; nieuwe modules hebben
geen Pyright-warnings; de warning-baseline is lager dan bij de start.

### 5.6 Release-readiness

- voer `pip-audit`, CycloneDX en Trivy uit op het release-artefact;
- controleer dubbele/onnodige dependencies en lockfile-consistentie;
- werk README, ARCHITECTURE, DATABASE en UPGRADE bij;
- voer staging smoke tests en rollback/runbook-review uit;
- controleer OpenAPI diff en migration chain als laatste gate.

## Volgorde

```text
5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6
```

De testharnas-voorbereiding voor 5.4 mag parallel starten met 5.2 en 5.3.
De eindverificatie vindt pas plaats wanneer beide extracties gemerged zijn.

## Buiten scope

- Geen nieuwe publieke API-contracten.
- Geen microservices of deployment-splitsing.
- Geen destructieve productiedowngrades.
- Geen functionele wijzigingen aan connectoren of exporters.

## Release-acceptatie

Release 5 is gereed wanneer de twee grote legacy-modules hun grenzen hebben,
unit/integration/migration/E2E groen zijn, query- en benchmarkgates slagen,
de Pyright-baseline is verlaagd, OpenAPI ongewijzigd compatibel blijft en de
security-, staging- en rollbackchecks zijn afgerond.
