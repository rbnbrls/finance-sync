# Release 12 plan — opruimen en formele release-validatie

## Doel

Release 12 sluit de modularisering af en maakt de release-gates formeel
reproduceerbaar. De domeinservices en concrete persistence-componenten zijn
aanwezig; deze release verwijdert de resterende legacyblokken, valideert de
gedragspariteit en levert het bewijs voor PostgreSQL, Redis, E2E, security en
staging.

Er komen geen nieuwe endpoints, connectoren, exporters of productfeatures.
Publieke API-responses, tenant-scoping, idempotentie, outbox-semantiek,
cursors en UnitOfWork-grenzen blijven ongewijzigd.

## Uitgangspositie

- Read-services bestaan voor accounts, portfolio/holdings, securities en
  analytics; query budgets en `QueryCounter` bestaan.
- `read_api.py` is nog circa 2.237 regels en bevat onbereikbare legacy-SQL na
  delegatieblokken.
- `orchestrator.py` bevat nog circa 2.107 regels en private `_upsert_*`/
  `_resolve_*`-methodes die alleen backward-compatible tests bedienen.
- Concrete account-, transaction- en holding-persistence wordt door de
  pipeline gebruikt.
- Lokale unit/regressietests zijn groen; Docker ontbreekt lokaal.
- Pyright staat op 69 warnings.

## Prioriteiten

| Prioriteit | Werkpakket | Definition of done |
|---|---|---|
| Kritiek | Legacy cleanup | Read-facade en orchestrator bevatten alleen delegatie/coördinatie; characterization tests blijven groen. |
| Kritiek | Real-service gates | Migration, PostgreSQL 16, Redis 7 en E2E draaien in CI zonder onverwachte skips. |
| Kritiek | Release evidence | OpenAPI, test-, benchmark-, scan- en staging-artifacts zijn opgeslagen. |
| Gemiddeld | Performance | Querybudgetten en latency-baselines zijn tegen echte DB-sessies vastgelegd. |
| Gemiddeld | Type debt | Warningbaseline daalt onder 69. |
| Laag | Documentatie | Upgrade-, rollback-, staging- en operationele runbooks zijn actueel. |

## Uitvoeringsfases

### 12.1 Characterization lock en legacy cleanup

1. Controleer alle delegatieroutes met response-, scope- en error-
   characterization tests.
2. Verwijder onbereikbare legacyimplementaties uit `read_api.py`.
3. Verwijder de private `_upsert_*`- en `_resolve_security_reference`-
   implementaties uit `orchestrator.py`.
4. Verplaats eventuele legacy-unittests naar de concrete persistence- en
   read-componenttests; behoud publieke methodesignatures uitsluitend waar
   externe callers ze werkelijk gebruiken.
5. Stel harde limieten vast: `read_api.py` maximaal 300 regels en
   `orchestrator.py` maximaal 900 regels.

Acceptatie: geen domein-SQL of entity-persistence in de facades; alle lokale
tests en OpenAPI-output blijven gelijk.

### 12.2 Query- en benchmarkevidence

1. Koppel ieder read-endpoint aan een named `READ_QUERY_BUDGETS`-budget.
2. Draai portfolio-, holdings-, securities- en analyticsqueries tegen
   PostgreSQL met `QueryCounter`.
3. Gebruik de deterministische profielen voor 100 en 1.000 holdings.
4. Publiceer query count, latency, datasetgrootte, Python- en PostgreSQL-
   versie als CI-artifact.
5. Voeg een negatieve N+1-test toe die de gate aantoonbaar laat falen.

Acceptatie: budgets slagen op echte DB-sessies; latest prices blijven één
batchquery; latency is reproduceerbaar gedocumenteerd.

### 12.3 Migration-, PostgreSQL- en Redis-gates

1. Draai op PostgreSQL 16: `upgrade head → downgrade base → upgrade head`.
2. Test migration chain en indexen 0036/0037 op een lege database.
3. Draai integrationtests tegen PostgreSQL 16 en Redis 7 voor locks, webhook
   throttling, outbox, sync-idempotentie en persistence rollback.
4. Upload JUnit-, migration- en service-logs als CI-artifacts.
5. Maak onverwachte skips fouten; alleen expliciet opt-in/provider-tests
   mogen worden overgeslagen.

Acceptatie: alle gates slagen in CI en de artifacts zijn terugvindbaar per
commit.

### 12.4 E2E, security en image validation

1. Draai API → worker → outbox exactly-once E2E tegen PostgreSQL/Redis.
2. Voer `pip-audit`, CycloneDX en Trivy uit.
3. Controleer `.trivyignore` op expiratie en rationale.
4. Controleer OpenAPI diff, lockfile-consistentie en image build.
5. Valideer secrets/redaction/security-scanoutput in de releasepipeline.

Acceptatie: E2E en security-jobs zijn groen; afwijkingen hebben een
expliciete, tijdgebonden uitzondering.

### 12.5 Staging, documentatie en rollback

1. Draai staging smoke met uitsluitend synthetische financiële data.
2. Controleer readiness, health, sync, outbox en exporter smoke flows.
3. Werk README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook bij.
4. Documenteer dat rollback via application-image rollback en
   backward-compatible migrations verloopt, niet via productiedowngrade.
5. Maak een releasechecklist met eigenaar, commit, artifact-link en datum.

Acceptatie: staging smoke en rollbackprocedure zijn aantoonbaar uitgevoerd
of voorzien van een formeel CI/staging-bewijsstuk.

### 12.6 Type debt en closeout

- verlaag Pyright van 69 naar maximaal 60 warnings;
- houd alle nieuwe read/persistence-code warning-vrij;
- draai volledige unit-, integration- en E2E-suites;
- archiveer benchmark- en security-artifacts;
- markeer Release 11 en Release 12 pas daarna als compleet.

## Volgorde en afhankelijkheden

```text
12.1 → 12.2 → 12.3 → 12.4 → 12.5 → 12.6
       └──── benchmarkfixtures en CI-artifacts parallel ────┘
```

12.1 is een harde poort: gates mogen niet draaien op code met dubbele
implementaties. 12.3 en 12.4 vereisen CI-services. 12.5 vereist geslaagde
service-gates. 12.6 is de formele releasebeslissing.

## Buiten scope

- Geen nieuwe publieke API-contracten of productfeatures.
- Geen nieuwe connectors, exporters of providerintegraties.
- Geen microservice-extractie.
- Geen echte financiële data in tests of staging.
- Geen destructieve productiedowngrade.

## Release-acceptatie

Release 12 is gereed wanneer:

1. de facades geen onbereikbare legacylogica meer bevatten;
2. de lokale suite, migration-, integration- en E2E-gates groen zijn;
3. querybudgetten en benchmarkevidence beschikbaar zijn;
4. security-, SBOM-, image- en OpenAPI-checks slagen;
5. staging smoke en rollbackdocumentatie compleet zijn;
6. Pyright maximaal 60 warnings rapporteert.

