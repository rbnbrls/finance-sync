# Release 9 plan — afronding en productieverificatie

## Doel

Release 9 maakt de modularisering uit Releases 3–8 daadwerkelijk af en sluit
de ontbrekende verificatiegates. De publieke API-contracten, tenant-scoping,
idempotentie, cursors, outbox-semantiek en UnitOfWork-grenzen blijven
ongewijzigd.

Er worden geen nieuwe endpoints, connectoren, exporters of productfeatures
toegevoegd. Deze release is uitsluitend gericht op decompositie,
performance-meetbaarheid, typekwaliteit en operationele betrouwbaarheid.

## Huidige uitgangspositie

- `services/read/accounts.py` en de set-based price-helper bestaan al.
- `sync/persistence.py` bestaat als expliciete stage-adapter, maar de concrete
  upsert-, change-detection- en outboxlogica staat nog in
  `sync/orchestrator.py`.
- `read_api.py` is nog circa 2.150 regels en bevat naast delegatie nog legacy
  portfolio-, securities- en analyticslogica.
- `sync/orchestrator.py` is nog circa 2.100 regels en bevat nog entity-
  persistence.
- De lokale unit/regressiesuite is groen; PostgreSQL-, Redis- en E2E-gates
  zijn lokaal niet uitgevoerd omdat Docker niet beschikbaar is.
- De Pyright-baseline staat op 69 warnings.

## Prioriteiten

| Prioriteit | Onderwerp | Definition of done |
|---|---|---|
| Kritiek | Read-componenten | Portfolio, securities en analytics zijn zelfstandige services; `read_api.py` bevat alleen facade/delegatie. |
| Kritiek | Concrete persistence | Upsert, change detection en outbox-operaties zitten in `sync/persistence.py`; de orchestrator coördineert alleen. |
| Kritiek | Real-service gates | PostgreSQL 16, Redis 7, migrations, integration en E2E draaien in CI zonder onverwachte skips. |
| Gemiddeld | Performance-gates | Query-budgetten en reproduceerbare benchmarks voor 100/1.000 holdings bestaan als CI-gate. |
| Gemiddeld | Type debt | Baseline daalt onder 69; nieuwe code blijft warning-vrij. |
| Laag | Release hygiene | OpenAPI diff, dependency/SBOM/image scans, staging smoke test en rollbackdocumentatie zijn gecontroleerd. |

## Uitvoeringsfases

### 9.1 Characterization en gedeelde read-contracten

1. Leg de bestaande response-, scope-, as-of-, freshness- en coverage-
   contracten vast met characterization tests.
2. Maak een gedeeld `ReadScope`-contract voor tenant-, account- en
   connection-scoping.
3. Maak gedeelde response-/metadata-contracten zodat componenten niet van
   `read_api.py` hoeven te importeren.
4. Definieer per endpoint de verwachte querybudgetten voordat de SQL wordt
   verplaatst.

Acceptatie: alle bestaande OpenAPI-responses blijven gelijk en de tests
leggen zowel normale als lege/stale/unpriced situaties vast.

### 9.2 Portfolio-readservice

1. Extraheer portfolio, holdings, balances, freshness en valuation naar
   `services/read/portfolio.py`.
2. Laat portfolio- en holdingsroutes uitsluitend via deze service lopen.
3. Verwijder de oude portfolio-SQL en mappinglogica uit `read_api.py`.
4. Test tenant-, account- en connection-scope, valuta, stale prices,
   unpriced holdings en empty-result gedrag.

Acceptatie: portfolio-component zelfstandig testbaar; geen portfolio-SQL in
de facade; OpenAPI diff bevat geen contractwijziging.

### 9.3 Securities- en analytics-readservices

Extraheer:

- `services/read/securities.py` voor securities, listings en prices;
- `services/read/analytics.py` voor history, net-worth en cashflow;
- gedeelde pagination-, freshness- en coverage-contracten.

Gebruik de bestaande pagination- en set-based price-helper. Componenten
ontvangen alleen hun expliciete session/scope-dependencies en importeren geen
facade-logica.

Acceptatie: routes delegeren naar componenten; `read_api.py` is maximaal 300
regels facade-/compatibiliteitscode; componenttests dekken scope, as-of,
currency conversion, missing prices en pagination.

### 9.4 Concrete persistence extraction

1. Verplaats `_upsert_account`, `_upsert_transaction` en `_upsert_holding`
   naar een concrete `SyncPersistence`-implementatie.
2. Verplaats security resolution naar dezelfde persistence dependency of een
   expliciete `SecurityResolver` dependency.
3. Houd tenant-id en provider-context expliciet in de persistence-interface.
4. Laat stages uitsluitend deze interface gebruiken.
5. Verwijder de duplicerende entity-persistence uit `orchestrator.py`.
6. Bewaak dat stages nooit committen, flushen buiten de afgesproken write
   operaties of een eigen UnitOfWork maken.

Acceptatie: tests dekken create, changed update, unchanged update, outbox
create/update, duplicate sync, unresolved security en rollback. De
orchestrator bevat geen entity-upsertdetails.

### 9.5 Query budgets en benchmarks

1. Voeg een SQLAlchemy query-count fixture toe voor portfolio, securities,
   history, net-worth en cashflow.
2. Stel versioneerbare budgetten vast per endpoint en datasetgrootte.
3. Bouw deterministische fixtures voor 100 en 1.000 holdings, meerdere
   accounts en ontbrekende/stale prices.
4. Meet query count en latency met expliciete hardware- en database-
   aannames; publiceer de baseline als CI-artifact.
5. Laat een kunstmatige N+1-regressie de test aantoonbaar laten falen.

Acceptatie: budgets zijn reproduceerbaar, latest prices blijven één
batch-query en geen endpoint overschrijdt de vastgelegde grens.

### 9.6 Real-service en release gates

1. Draai migrations op een lege PostgreSQL 16-database.
2. Valideer `upgrade head → downgrade base → upgrade head`.
3. Draai integrationtests met PostgreSQL 16 en Redis 7 voor locks, rate
   limiting, outbox, sync-idempotentie en migration `0037`.
4. Draai de volledige API → worker → outbox E2E-suite.
5. Maak onverwachte skips CI-fouten; alleen expliciet gemotiveerde opt-in
   connector-tests mogen worden overgeslagen.
6. Documenteer de lokale Docker Compose-route wanneer Docker beschikbaar is;
   CI blijft de formele gate.

Acceptatie: alle real-service suites zijn groen en artifacts bevatten logs,
migration-output en testresultaten.

### 9.7 Type, security en release hygiene

- los warnings op tot de baseline onder 69 komt;
- voer Ruff, Pyright, pip-audit, CycloneDX en Trivy uit;
- controleer OpenAPI tegen de vorige release;
- valideer Alembic chain, lockfile en image build;
- werk README, ARCHITECTURE, DATABASE, UPGRADE en rollback-runbook bij;
- voer een staging smoke test uit met alleen synthetische financiële data.

Acceptatie: alle releasechecks zijn geautomatiseerd of voorzien van een
vastgelegd, reproduceerbaar bewijsstuk.

## Volgorde en afhankelijkheden

```text
9.1 → 9.2 → 9.3 → 9.4 → 9.5 → 9.6 → 9.7
       └──────────────┬──────────────┘
      testfixtures en query-gates parallel voorbereiden
```

9.1 is verplicht vóór de read-extracties om contractdrift te voorkomen. 9.4
start pas nadat de stage-contracten en regressietests stabiel zijn. 9.6 en
9.7 zijn eindgates en mogen geen openstaande functionele extracties maskeren.

## Buiten scope

- Geen nieuwe publieke API-contracten of productfeatures.
- Geen nieuwe connectors, exporters of providerintegraties.
- Geen microservice-extractie.
- Geen productiedowngrade als rollbackstrategie.
- Geen gebruik van echte financiële data in fixtures of staging smoke tests.

## Release-acceptatie

Release 9 is gereed wanneer:

1. `read_api.py` uitsluitend facade-/delegatielogica bevat;
2. `sync/orchestrator.py` uitsluitend pipelinecoördinatie bevat;
3. persistence- en read-componenten onafhankelijk getest zijn;
4. query budgets en benchmarks als CI-gates slagen;
5. PostgreSQL/Redis integration-, migration- en E2E-tests groen zijn;
6. de Pyright-baseline onder 69 ligt;
7. security-, OpenAPI-, staging- en rollbackchecks gedocumenteerd zijn.

