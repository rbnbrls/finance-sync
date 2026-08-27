# Release 4 plan — volledige decompositie en quality gates

## Doel

Release 4 rondt de nog openstaande Release 3-werkzaamheden af en maakt de
kwaliteitseisen reproduceerbaar in CI. De release verandert geen publieke
REST-contracten en introduceert geen nieuwe productfeatures. De focus ligt op
modulariteit, echte PostgreSQL/Redis-verificatie en het afdwingen van de
bestaande roadmap-eisen.

## Openstaande uitgangssituatie

- `services/read_api.py` is nog ongeveer 2.100 regels en bevat naast de
  facade nog domeinlogica voor accounts, portfolio, securities en analytics.
- `sync/orchestrator.py` is nog ongeveer 2.000 regels en bevat pipeline-
  coördinatie, provider-mapping, persistence en foutafhandeling tegelijk.
- Pyright heeft 69 warnings; fouten zijn afwezig, maar de warning-baseline
  wordt nog niet als CI-budget bewaakt.
- De unit-suite is groen, maar de echte PostgreSQL/Redis integration-suite is
  lokaal niet gevalideerd in de Release 3-run.
- Query-budgettests, benchmarks en volledige migratie-upgrade-verificatie
  ontbreken nog als release-gates.

## Prioriteiten

| Prioriteit | Onderwerp | Definition of done |
|---|---|---|
| Kritiek | Read-service decompositie | Facade + portfolio, securities en analytics services; geen domeinquerylogica meer in de facade. |
| Kritiek | Sync-stage decompositie | Authenticate, accounts, transactions, holdings en persistence zijn afzonderlijk testbaar. |
| Kritiek | Integration/migration gate | Ephemeral PostgreSQL en Redis draaien in CI; upgrade/downgrade en kernflows slagen. |
| Gemiddeld | Query budgets en benchmarks | Vastgelegde querylimieten en regressiedrempels voor grote portfolio's en syncs. |
| Gemiddeld | Type warning budget | Geen nieuwe warnings; warnings in gewijzigde modules worden verwijderd. |
| Laag | Dependency/documentatie-hygiëne | Audit/SBOM, modulekaart, upgrade-notes en release-runbook zijn bijgewerkt. |

## Uitvoeringsfasen

### 4.1 Contract- en migratievoorbereiding

1. Voeg characterization tests toe voor alle publieke `ReadService`-methodes,
   response-modellen, foutcodes, tenant-scoping en sorteer/paginatiegedrag.
2. Leg de huidige query-counts vast voor portfolio, securities, net-worth en
   cashflow.
3. Voeg een migration test toe die op een lege PostgreSQL-database van
   `base` naar `head` migreert en terug kan downgraden.
4. Maak een expliciete baseline voor Pyright-waarschuwingen en registreer
   die in CI.

Acceptatie: de baseline-tests slagen vóór de eerste verplaatsing en falen
wanneer response-shapes, query-budgetten of migratieketen onbedoeld wijzigen.

### 4.2 Read-service volledig decomponeren

Maak de volgende modules leidend:

- `services/read/facade.py`: compatibiliteitslaag voor API en legacy callers;
- `services/read/accounts.py`: account- en transactielijsten;
- `services/read/portfolio.py`: portfolio, holdings en balances;
- `services/read/securities.py`: securities, listings en prices;
- `services/read/analytics.py`: history, net-worth en cashflow;
- `services/read/pagination.py` en `prices.py`: gedeelde queryhelpers.

Verplaats functies met behoud van bestaande DTO's. De facade mag alleen
scope/context construeren en de juiste component aanroepen. Iedere component
krijgt een expliciet getypeerde session en `ReadScope`; geen directe import
van API-routecode.

Acceptatie:

- `read_api.py` bevat geen SQL-querylogica meer;
- alle bestaande read- en top-level endpointtests blijven groen;
- OpenAPI diff bevat geen breaking changes;
- tenant- en account-scoping wordt per component getest;
- query-counts blijven gelijk of verbeteren.

### 4.3 SyncOrchestrator volledig decomponeren

Introduceer:

- `sync/context.py` met een immutable/getypeerde run-context;
- `sync/stages/authenticate.py`;
- `sync/stages/accounts.py`;
- `sync/stages/transactions.py`;
- `sync/stages/holdings.py`;
- `sync/persistence.py` voor upsert/change detection/outbox;
- `sync/errors.py` als centrale classificatiegrens;
- een orchestrator die alleen stagevolgorde, UoW en resultaataggregatie beheert.

Stages ontvangen geen globale service-state en voeren geen autonome commits
uit. Provider-specific mapping blijft in connectors of expliciete mapper-
modules. De bestaande cursor-, rollback-, retry- en outbox-semantiek blijft
intact.

Acceptatie:

- iedere stage is afzonderlijk unit-testbaar;
- account-, transaction- en holdings-fouten testen rollback en failed SyncRun;
- transient/permanent errors behouden hun retrygedrag;
- dubbele syncs produceren geen dubbele facts/events;
- `orchestrator.py` is maximaal coördinatiecode en blijft onder 300 regels.

### 4.4 Echte integration-, performance- en migratiegates

1. Laat CI ephemeral PostgreSQL en Redis starten voor `pytest -m integration`.
2. Test repositories, outbox, sync-orchestrator, Redis locks/rate limits,
   webhook throttling en migratie upgrade/downgrade tegen de echte services.
3. Voeg query-budgettests toe die per endpoint maximaal één query voor latest
   prices en geen N+1-patroon toestaan.
4. Voeg reproduceerbare benchmarks toe voor een portfolio met 100/1.000
   holdings en een sync met meerdere accounts.
5. Definieer regressiedrempels en documenteer hardware-/dataset-aannames.

Acceptatie: integration-tests slagen in een schone CI-run; benchmarkresultaten
zijn opgeslagen als baseline; een query- of latency-regressie boven de grens
faalt de kwaliteitsgate.

### 4.5 Typekwaliteit en CI-budgetten

1. Verwijder de warnings in alle Release 4-modules.
2. Los daarna warnings op in gewijzigde bestaande modules, vooral
   `reportArgumentType`, `reportOptionalMemberAccess` en private API-gebruik.
3. Laat CI de Pyright-warningcount vergelijken met een versioned baseline en
   alleen daling of gelijkstand accepteren.
4. Voeg typeprotocollen toe voor stage-context, session factory en Redis.

Acceptatie: Pyright heeft nul fouten, nieuwe modules hebben nul warnings en
het warning-budget kan niet ongemerkt stijgen.

### 4.6 Dependency, documentatie en release-readiness

- controleer dubbele/losse dependencies in `pyproject.toml` en lockfile;
- voer dependency-audit, SBOM- en image-scan uit op het release-artefact;
- werk `ARCHITECTURE.md`, `DATABASE.md`, `README.md` en `UPGRADE.md` bij;
- voeg een rollback/runbook-checklist toe voor de nieuwe CI integration-gate;
- documenteer de nieuwe modulegrenzen en query/performance-signalen.

## Volgorde en parallelisatie

De veilige volgorde is:

```text
4.1 → 4.2 → 4.3 → 4.4 → 4.5 → 4.6
```

4.4 kan de testharnas-infrastructuur parallel voorbereiden tijdens 4.2 en
4.3, maar de eindgates worden pas uitgevoerd nadat beide decomposities klaar
zijn. Elke fase eindigt met:

```text
ruff format/check → pyright → unit tests → integration tests
→ OpenAPI diff → migration check → query/benchmark gate → git diff review
```

## Buiten scope

- Geen nieuwe connectors, exporters, endpoints of databasefeatures.
- Geen microservice-extractie; de modular monolith blijft de deployment-eenheid.
- Geen publieke foutcode- of responsewijziging zonder afzonderlijk contract-
  besluit.
- Geen downgrade van productie als standaard rollback; image rollback blijft
  leidend, met backward-compatible migrations.

## Release-acceptatie

Release 4 is gereed wanneer:

1. beide grote modules hun afgesproken grenzen hebben en de facade-/stage-
   contracten getest zijn;
2. unit-, integration- en migration-suites groen zijn;
3. OpenAPI diff geen onbedoelde breaking change bevat;
4. query budgets en benchmarks geen regressie tonen;
5. Pyright geen fouten heeft en de warning-baseline niet stijgt;
6. dependency/SBOM/image-scan en staging smoke tests slagen;
7. documentatie en rollback/runbook-review zijn afgerond.
