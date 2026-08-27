# Release 3 plan — modulariteit en onderhoudbaarheid

## Doel

Release 3 maakt de grootste resterende onderhoudbaarheidsproblemen kleiner
zonder een functionele API- of datamigratiebreuk. De focus ligt op het
opsplitsen van de twee grote service-modules, voorspelbare foutafhandeling,
typekwaliteit en structurele regressiebescherming.

De wijzigingen worden in kleine, onafhankelijk testbare stappen uitgevoerd.
Elke stap behoudt de bestaande REST-schema's, idempotentie, tenant-scoping en
transactionele grenzen.

## Scope en prioriteit

| Prioriteit | Onderwerp | Resultaat |
|---|---|---|
| Kritiek | Read API decompositie | `read_api.py` wordt een dunne facade boven domeinspecifieke read-services. |
| Kritiek | SyncOrchestrator decompositie | Sync-fases, persistence en foutclassificatie krijgen expliciete grenzen. |
| Gemiddeld | Error handling en observability | Geen ongerichte foutlekken; logs bevatten context en zijn veilig geredigeerd. |
| Gemiddeld | Typekwaliteit | Nieuwe code is strict typed; bestaande Pyright-waarschuwingen worden systematisch afgebouwd. |
| Gemiddeld | Test- en performance-gates | Query-aantallen, idempotentie, rollback en migratieketen worden expliciet bewaakt. |
| Laag | Documentatie en dependency hygiene | Architectuur, upgrade-notes en afhankelijkheidsbeleid sluiten aan op de code. |

## Implementatiefases

### 3.1 Contracts en nulmeting

1. Leg bestaande publieke methodes, response-schema's en foutcodes van
   `ReadService` vast.
2. Leg de huidige `SyncOrchestrator`-fasen en transactionele grenzen vast.
3. Voeg tijdelijke query-count/latency-tests toe voor portfolio-, security-
   en sync-read flows.
4. Maak een dependency-map: API → services → repositories/models → adapters.

Acceptatie: er is een baseline voor response-shapes, query-aantallen,
doorlooptijd en sync-idempotentie; geen gedrag wordt in deze fase gewijzigd.

### 3.2 Read API opsplitsen

Introduceer een `services/read/`-structuur met ten minste:

- `portfolio.py` voor holdings, cash en portfolio-overzichten;
- `securities.py` voor security-lijsten en latest-price reads;
- `analytics.py` voor allocatie, performance, net-worth en cashflow;
- `pagination.py` voor gedeelde cursor/page-validatie;
- `facade.py` of een kleine compatibiliteitslaag die de bestaande
  `ReadService`-interface behoudt.

Verplaats functies per domein, injecteer alleen de benodigde session/repository
afhankelijkheden en verwijder dubbele query- en mappinglogica. De bestaande
`services/read/prices.py` blijft de gedeelde implementatie voor latest-price
queries.

Acceptatie:

- REST-endpoints en OpenAPI-schema's blijven additief/ongewijzigd;
- tenant-filters en autorisatie blijven op dezelfde lagen afdwingbaar;
- bestaande read-tests blijven groen;
- query-count tests tonen geen nieuwe N+1-patronen;
- `read_api.py` bevat uitsluitend facade-/compositiecode.

### 3.3 SyncOrchestrator opsplitsen

Splits `sync/orchestrator.py` in expliciete componenten, bijvoorbeeld:

- `sync/stages/authenticate.py` voor connector-authenticatie en state;
- `sync/stages/accounts.py` voor account upsert en events;
- `sync/stages/transactions.py` voor transacties, cards en scheduled payments;
- `sync/stages/holdings.py` voor security resolution en holdings;
- `sync/persistence.py` voor upsert/change-detection/outbox helpers;
- `sync/errors.py` voor transient/permanent/domain error mapping;
- een kleine `SyncOrchestrator` die alleen volgorde, context en transactionele
  coördinatie beheert.

Behoud één expliciete sync-context met tenant, connection, run-id, cursor en
structured logger. Behoud de bestaande rollback- en outbox-semantiek; een
stage mag geen autonome commit uitvoeren.

Acceptatie:

- account-, transaction- en holding-stages zijn afzonderlijk unit-testbaar;
- een fout in iedere stage markeert de SyncRun correct en rolt de batch terug;
- retries classificeren transient/permanent fouten hetzelfde als voorheen;
- herhaalde syncs maken geen dubbele facts of outbox-events;
- `orchestrator.py` bevat geen provider-specifieke mappingdetails meer.

### 3.4 Foutafhandeling en logging normaliseren

1. Definieer een kleine exception-taxonomie voor validatie, dependency,
   provider, persistence en authorization errors.
2. Vervang brede `except Exception`-blokken in domeinservices door specifieke
   catches; brede catches blijven alleen aan API/worker-boundaries voor
   logging, rollback en veilige response-mapping.
3. Gebruik vaste structured-log events met `tenant_id`, `run_id`,
   `connection_id`, `provider`, `duration_ms` en `error_type` waar relevant.
4. Redigeer URLs, credentials, raw provider payloads en financiële details
   vóór logging.
5. Maak retry/backoff en terminale fouten zichtbaar in health/metrics zonder
   exception-teksten naar clients door te geven.

Acceptatie: voor iedere publieke fout is de HTTP-status en veilige foutcode
gedocumenteerd; logs bevatten correlation context maar geen secrets of raw
financial payloads; worker retries blijven begrensd en observeerbaar.

### 3.5 Typekwaliteit verhogen

1. Maak protocols/types voor session factories, Redis-clientgebruik,
   connector capabilities en stage-contexten.
2. Vervang `Any`, ongerichte `cast` en `type: ignore` in gewijzigde modules
   door concrete types of lokaal gemotiveerde adapters.
3. Ruim de bestaande Pyright-waarschuwingen gefaseerd op, te beginnen met
   `reportArgumentType`, `reportOptionalMemberAccess` en private-API gebruik.
4. Stel een CI-regel in dat het aantal Pyright-waarschuwingen niet mag
   toenemen; maak daarna de grens nul voor de nieuwe Release 3-modules.

Acceptatie: `pyright -p pyproject.toml src` heeft geen fouten, nieuwe modules
hebben geen warnings, en CI bewaakt de waarschuwing-baseline.

### 3.6 Testen, performance en documentatie

Voeg toe:

- contracttests voor de read-facade en bestaande response-schema's;
- stage-tests voor success, transient failure, permanent failure en rollback;
- integration-tests tegen PostgreSQL/Redis voor migratie `0037`, Redis webhook
  throttling en query-indexgedrag;
- query-budgettests voor portfolio/security endpoints;
- een kleine benchmark voor grote portfolio's en syncs met meerdere accounts;
- dependency-audit/SBOM-controle en documentatie van updatebeleid;
- bijgewerkte `ARCHITECTURE.md`, `DATABASE.md`, `README.md` en
  `UPGRADE.md` met de nieuwe modulegrenzen en operationele signalen.

## Volgorde en gates

De aanbevolen volgorde is `3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6`.
3.2 en 3.3 mogen parallel worden voorbereid, maar niet tegelijk dezelfde
publieke servicefuncties verplaatsen. Iedere fase eindigt met:

```text
ruff format/check → pyright → unit tests → relevante integration tests
→ OpenAPI diff → migration check → git diff review
```

## Buiten scope

- Geen nieuwe connectoren, exporters of REST-endpoints.
- Geen gedistribueerde service-extractie buiten de modular monolith.
- Geen destructieve databasewijzigingen; nieuwe schemawijzigingen volgen
  expand/contract.
- Geen wijziging van publieke foutcodes of payloads zonder afzonderlijk
  contractbesluit.

## Release-acceptatie

Release 3 is klaar wanneer:

1. de read- en sync-modules hun afgesproken grenzen hebben;
2. de volledige unit-suite en PostgreSQL/Redis integration-suite slagen;
3. OpenAPI geen onbedoelde breaking changes bevat;
4. Pyright geen fouten heeft en de warning-baseline niet is verhoogd;
5. migratie `0037` en eventuele nieuwe migraties op een lege én bestaande
   database kunnen worden uitgerold;
6. staging smoke tests, query-budgettests en rollback/runbook-review slagen.

