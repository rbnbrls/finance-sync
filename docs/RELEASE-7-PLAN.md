# Release 7 plan — read-/sync-afronding en schaalbare verificatie

## Doel

Release 7 voltooit de modularisering die in Release 6 is gestart. De
portfolio-, securities- en analytics-readservices worden uit de legacy
`ReadService` gehaald; holdings en persistence worden aparte sync-stages. De
release sluit af met query-/benchmarkgates en real-service verificatie.

Er komen geen nieuwe endpoints, connectoren of exporters. Bestaande API-
responses, tenant-scoping, idempotentie, cursors en UnitOfWork-grenzen blijven
ongewijzigd.

## Openstaande punten

- `read_api.py` bevat nog ruim 2.000 regels legacy-query- en mappinglogica.
- `sync/orchestrator.py` bevat nog holdings-, persistence- en coördinatielogica.
- Account- en transactiestages bestaan; holdings en persistence nog niet.
- Query-budgettests beperken zich tot latest prices; endpoint-budgets en
  benchmarks voor 100/1.000 holdings ontbreken.
- PostgreSQL/Redis integration- en E2E-gates zijn lokaal niet uitgevoerd door
  het ontbreken van Docker.
- De Pyright-warningbaseline staat op 69.

## Prioriteiten

| Prioriteit | Onderwerp | Definition of done |
|---|---|---|
| Kritiek | Portfolio-readservice | Holdings, balances en portfolio lopen via een eigen component. |
| Kritiek | Securities/analytics-readservices | Securities, prices, history, net-worth en cashflow zijn los testbaar. |
| Kritiek | Holdings/persistence stages | Orchestrator bevat alleen pipelinecoördinatie en transactionele grenzen. |
| Gemiddeld | Query budgets/benchmarks | N+1 en schaalregressies falen automatisch. |
| Gemiddeld | Real-service gates | CI bewijst migration, integration en E2E zonder onverwachte skips. |
| Laag | Type/release hygiene | Warning-baseline daalt; docs, scans en rollbackchecks zijn bijgewerkt. |

## Uitvoeringsfases

### 7.1 Portfolio-readservice

1. Maak `services/read/portfolio.py` eigenaar van portfolio, holdings en
   balances.
2. Behoud bestaande DTO's en `ReadScope`-filters.
3. Laat routes en facade de component gebruiken en verwijder de dubbele
   legacy-implementaties uit `read_api.py`.
4. Voeg tests toe voor tenant-scope, account-scope, stale/unpriced holdings,
   valuta en portfolio-meta.

Acceptatie: portfolio- en holdings-endpoints zijn contractcompatibel,
`read_api.py` bevat deze SQL niet meer en componenttests dekken alle
scope-/freshness-varianten.

### 7.2 Securities- en analytics-readservices

Extraheer:

- `services/read/securities.py` voor securities, listings en prices;
- `services/read/analytics.py` voor portfolio-history, net-worth, cashflow en
  gedeelde as-of/freshness/coverage metadata.

Gebruik de bestaande set-based price helper en `pagination.py`. Verwijder
duplicated mappinglogica en houd de facade beperkt tot scope/context en
delegatie.

Acceptatie: elke component is afzonderlijk unit-testbaar, OpenAPI blijft
ongewijzigd compatibel en `read_api.py` is maximaal 300 regels facade-/DTO-
compatibiliteitscode.

### 7.3 Holdings- en persistence-stages

1. Voeg `sync/stages/holdings.py` toe voor holdings fetch, security resolution,
   unresolved queue en holding persistence.
2. Voeg `sync/persistence.py` toe voor gedeelde upsert/change-detection/outbox
   grenzen.
3. Laat stages alleen werken binnen de door de orchestrator aangeleverde
   UnitOfWork; geen stage mag committen.
4. Behoud cursor-advance, rollback, retry-classificatie en event-idempotentie.

Acceptatie: holdings-, persistence-, rollback- en duplicate-sync-tests slagen;
`orchestrator.py` bevat geen entity-upsertdetails meer en blijft maximaal 300
regels coördinatiecode.

### 7.4 Query-budgettests en benchmarks

- Voeg een SQLAlchemy query-count fixture toe voor portfolio, securities,
  net-worth en cashflow.
- Leg per endpoint een budget vast; latest prices blijven één batch-query.
- Voeg reproduceerbare datasets en benchmarks toe voor 100 en 1.000 holdings
  en meerdere sync-accounts.
- Documenteer hardware, dataset, meetmethode en toegestane afwijking.

Acceptatie: een kunstmatig ingevoegde N+1-query of latency-/query-regressie
faalt de gate; baseline-uitkomsten worden als CI-artifact bewaard.

### 7.5 PostgreSQL/Redis migration- en E2E-gates

1. Draai de bestaande integration-suite tegen PostgreSQL 16 en Redis 7.
2. Valideer `upgrade head → downgrade base → upgrade head`, inclusief index
   `0037`.
3. Controleer Redis-webhook throttling, locks, outbox en sync-idempotentie.
4. Laat de volledige API → worker → outbox E2E-suite slagen zonder onverwachte
   skips.
5. Documenteer Docker Compose voor lokaal uitvoeren; CI is de formele gate
   wanneer Docker lokaal niet beschikbaar is.

### 7.6 Type debt en release-readiness

- verlaag de Pyright-baseline onder 69 en houd nieuwe modules warning-vrij;
- voer dependency-audit, CycloneDX en Trivy uit;
- controleer OpenAPI diff en migratieketen;
- werk README, ARCHITECTURE, DATABASE, UPGRADE en rollback-runbook bij;
- voer staging smoke tests uit vóór promotion.

## Volgorde

```text
7.1 → 7.2 → 7.3 → 7.4 → 7.5 → 7.6
```

De integration-infrastructuur en benchmarkfixture kunnen parallel worden
voorbereid tijdens 7.1–7.3. De eindgates lopen pas na de extracties.

## Buiten scope

- Geen microservice-extractie of deployment-splitsing.
- Geen publieke contractwijzigingen of nieuwe productfeatures.
- Geen nieuwe connectoren of exporters.
- Geen destructieve productiedowngrade als rollbackstrategie.

## Release-acceptatie

Release 7 is gereed wanneer `read_api.py` en `sync/orchestrator.py` alleen nog
compatibiliteits-/coördinatiecode bevatten, alle componenten en stages
afzonderlijk getest zijn, unit/integration/migration/E2E groen zijn,
query-/benchmarkgates slagen, de Pyright-baseline daalt en security-, OpenAPI-,
staging- en rollbackchecks zijn afgerond.
