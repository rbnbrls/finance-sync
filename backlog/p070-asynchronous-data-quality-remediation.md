---
title: "Asynchronous data-quality remediation pipeline"
status: done
priority: 70
---

## Realisatiestatus (bijgewerkt 2026-09-12)

De eerste verticale slice is geïmplementeerd en getest. De capability is nog
niet volledig klaar volgens de definition of done; onbekende of nog niet
geadopteerde strategies gaan bewust naar `manual_review`.

### Gereed

- Durable model `DataQualityRemediationItem` en Alembic migration `0068` met
  tenant/credential foreign keys, unieke `(tenant_id, deduplication_key)` en
  due/lease/provider/batch-indexes.
- Immutable `DetectedIssue`, stabiele genormaliseerde SHA-256 sleutel en
  atomische PostgreSQL upsert voor deduplicatie.
- Tenant-scoped backlog list/detail, bounded due claiming met
  `FOR UPDATE SKIP LOCKED`, claim token, lease en token-gecontroleerde
  transitions. De bounded claim-candidate set wordt na locking provider-
  fair geïnterleaved, zodat één provider de batch niet kan monopoliseren.
- De worker commit de claim vóór connectorconstructie/provider-I/O; bij een
  tenantfout wordt de lopende transaction teruggedraaid zodat volgende
  tenants niet worden geblokkeerd.
- De claim-lease is minimaal de maximale execution-timeout plus buffer, zodat
  een bounded call niet door een normale lease-expiry dubbel geclaimd wordt.
- Due claims gebruiken een uitlegbare scheduling-score met severity, priority,
  begrensde leeftijd, attempt-penalty en request-cost, waarna provider-fair
  wordt geïnterleaved.
- Remediation heeft nu een eigen enable/poll-configuratie met bounded
  execution-timeout, verification-attempt cap en configureerbare retry
  base/cap/jitter; de oude repair-enable/interval aliases blijven tijdelijk
  backwards-compatible.
- Lifecycle states, retry/error classification en jittered exponential
  backoff; stale processing leases zijn opnieuw claimbaar.
- Operator-transitions zijn ook source-state begrensd: retry heractiveert
  alleen retrybare/deferred/manual-review/failed items en ignore kan geen
  reeds terminal `resolved`/`ignored` item terug wijzigen.
- Authentication/credential failures worden afzonderlijk geclassificeerd en
  gaan naar `manual_review`; runtime-429's gaan naar `deferred` met
  `Retry-After` en verhogen de deferral-teller zonder normaal retry-budget.
- Een authentication-failure markeert de tenant-scoped `Credential` als
  `reauth_required` via de bestaande provider-healthvelden; alleen een vaste,
  bounded diagnostic wordt opgeslagen.
- Reconciliation finalize registreert findings als backlogwerk zonder
  connector-import of provider-call.
- `POST /control-plane/data-health/repair` enqueue't nu en retourneert `202`
  met item-ID's; er is list/detail/retry/requeue/ignore/priority API met
  tenant-scoping en bestaande permissions.
- De oude periodieke directe repair-loop is vervangen door een dunne
  remediation claim/execute tick met bounded settings. Zonder geregistreerde
  strategy wordt werk `manual_review`.
- Unit-tests voor stable keys, retry policy en bestaande reconciliation,
  control-plane en worker regressies, plus batch/quota-tests. De gerichte
  Plaid/remediation-gate staat op 41 groen; de volledige suite staat op 4.028
  groen met 236 skips.
- De volledige Alembic-keten inclusief revisions `0068` en `0069` genereert
  succesvol offline PostgreSQL SQL; migrations upgrade/downgrade zijn ook
  tegen PostgreSQL gevalideerd in de integration-gate.
- `make test-integration` provisioneert nu zelf de ephemeral PostgreSQL/Redis
  teststack en ruimt die ook op bij succes of failure. De cleanup-trap staat
  vóór het opstarten en ruimt dus ook gedeeltelijke starts op.
- De volledige PostgreSQL/Redis integration-gate is uitgevoerd tegen
  PostgreSQL 16 en Redis 7: `196 passed`, inclusief migrations, tenant-
  isolation, Redis semantics en de P070-concurrencytests.
- Redis-quota primitive met atomische Lua-reservation, provider/connection/
  endpoint scope, bounded `Retry-After` parsing en cooldown keys. De executor
  deferert quota-pressure, respecteert actieve provider-cooldowns en telt
  rate-limit deferrals op.
- De executor begrenst individuele en batch-executies met een timeout; een
  timeout wordt per item als bounded transient retry geregistreerd en laat
  geen onbegrensde provider-call doorlopen.
- Exceptions uit batchfactory/providerfetch/strategy worden per item
  geclassificeerd en transitioneren alle claims; geen batch-exception laat
  items onbedoeld in `processing` tot lease-expiry achter.
- Strategy-neutrale compatibility keys, batch splitting en aangrenzende
  dag-range coalescing; dezelfde bounded compatibility-key wordt nu ook als
  `batch_key` op ieder backlog-item opgeslagen voor indexeerbare planning.
- Data-health backlogaggregatie en Prometheus metric definitions voor backlog,
  throughput, deferred quota en batch-efficiency.
- Concrete `Trading212InstrumentMetadataStrategy` via de registry/worker:
  credential decryptie gebeurt pas tijdens execution, `fetch_instruments()`
  werkt alleen de betreffende `UnresolvedSecurity` bij en lokale verification
  bepaalt of het item `resolved` wordt.
- Provider-neutrale `LatestQuoteStrategy` via `EnrichmentGateway` is
  aangesloten op de backlog-worker. Holdings zonder prijs worden enqueue-only
  als `quote_gap` geregistreerd; execution gebruikt de bestaande OpenBB/local
  fallback en verification vereist een recente, niet-lege daily quote.
- Historical-price windows voor dezelfde security worden bounded
  samengevoegd tot één gateway-fetch; compatibele latest-quote-items delen
  eveneens één fetch. De compatibility key bevat security- en identifier-scope
  zodat verschillende securities nooit in één batch terechtkomen.
- Connectors kunnen nu backwards-compatible veilige remediation-capabilities
  declareren; de connector registry publiceert alleen key, endpoint family en
  batch limit, nooit credentials of callables.
- Transaction-history remediation is capability-driven aangesloten voor
  Trading212, Bunq, YNAB, DEGIRO Pensioen, SaxoInvestor, CSV import, manual
  expense en Plaid-like. Plaid-like production gebruikt een begrensde,
  injectable `/transactions/get` date-range fetch; sandbox behoudt fixtures.
- Worker gebruikt nu compatibility batching met per-item outcome-isolatie;
  verification counts worden begrensd en een issue dat na het maximum nog
  bestaat gaat naar `manual_review`. Iedere uitgevoerde verification bewaart
  bovendien een bounded reason en `verified_at`.
- Resolved re-detection heeft loop-prevention: bevestiging binnen 24 uur
  heropent niet, terwijl een latere detectie een nieuwe generation key krijgt.
- Retention cleanup verwijdert uitsluitend oude `resolved`/`ignored` items,
  is tenant-scoped uitvoerbaar en draait als aparte dagelijkse workerjob;
  actieve en `manual_review` items worden nooit automatisch verwijderd.
- Dezelfde cleanup verwijdert alleen remediation-specifieke operator-auditrows
  na de retentionperiode; algemene connection-auditdata wordt niet geraakt.
- Individuele retry-, ignore- en priority-mutaties schrijven nu, net als
  bulk-retry, een bounded gesanitiseerde operator-auditentry.
- Operator kan maximaal 100 tenant-scoped items in één bulk-retry requeue-en;
  de mutatie wordt vastgelegd in de bestaande gesanitiseerde audittrail.
- `POST /control-plane/remediation/backfill` voert een bounded, idempotente
  backfill uit voor historische reconciliation-findings met een expliciet
  strategycontract; onbekende findings blijven read-only.
- Worker publiceert backlogstatus, oudste due-itemleeftijd en manual-review
  size naar Prometheus; Grafana provisioning bevat alerts voor backlog-aging
  en manual-review pressure.
- Prometheus bevat nu ook een expliciete attempts-counter per provider/
  strategy; de observability-documentatie inventariseert throughput, quota,
  batch-efficiency en saved-call metrics. Provider- en status-gauges worden
  per tick gecleard en opnieuw opgebouwd, zodat verdwenen labels niet blijven
  hangen.
- De executor schrijft bounded structured outcome/failure- en batchlogs met
  backlog-ID, provider, connection, strategy, attempt, classification,
  schedulingtijd, batch-ID/-grootte en een hash van de claim-token; secrets en
  providerpayloads worden niet gelogd.
- Failed remediation outcomes hebben een aparte low-cardinality counter en
  Grafana-alert; `docs/observability.md` bevat de metric- en alertinventaris.
- Dashboard `Data health` toont backlogstatus, strategie, status per item en
  een tenant-scoped retryactie. De statuskaart toont ook provider/status-
  aggregaten; de repairknop blijft enqueue-only.
- `data_quality_remediation_pending_by_provider{provider}` wordt door de
  worker gepubliceerd naast de status-gauge.
- `tests/integration/test_remediation_concurrency_pg.py` bevat echte
  PostgreSQL/Redis-tests voor dedup-races, `SKIP LOCKED` claim-splitting en
  gedeelde quota; alle 3 tests zijn uitgevoerd tegen PostgreSQL 16 en Redis 7.
- `ReconciliationResult.remediation_item_id` koppelt een finding transactioneel
  aan het geregistreerde backlog-item; migration `0069` maakt deze relatie
  expand-only en indexeert haar voor traceability.

### Afgerond / bewust begrensd

- Historische findings zonder volledig expliciet window-/strategycontract
  blijven bewust read-only; providers zonder expliciete capability gaan naar
  `manual_review`. Dit is de veilige v1-eindgrens, geen onafgemaakte flow.
- Historical-price en latest-quote remediation zijn voor hun expliciete
  mappings aangesloten en gebatcht. De strategies weigeren incomplete scopes
  en hun verification controleert tenant-owned holdings, interval/window en
  geldige close-observaties.
- Scoped verification voor transaction-history (tenant/provider/account,
  connection, venster en niet-tombstoned rows) en price-history
  (tenant/security/interval, venster en geldige close-observaties) is
  aanwezig en getest. Andere finding-types blijven expliciet zonder strategy
  en worden veilig naar `manual_review` gestuurd.
- Automatische backfill van findings zonder expliciet strategycontract is
  bewust niet toegestaan; de veilige, idempotente backfill-entrypoint bestaat.
- De oude `DataQualityRepairService` blijft als backwards-compatible helper
  beschikbaar voor bestaande callers; er zijn geen productie-worker-calls
  meer naar de directe globale repairloop. Alle ondersteunde productie-adapters
  lopen via de backlog registry/executor.
- De functionele tests, Ruff en de strikte source-Pyright-check zijn groen;
  Pyright meldt geen errors of warnings op de gewijzigde Plaid/remediation
  modules. De volledige suite is succesvol afgerond; de gerichte
  Plaid/remediation-gate staat op 41 groen. De volledige integration-gate
  staat op 196 groen met 22 bestaande warnings; daarvan zijn de 3
  P070-concurrencytests expliciet tegen PostgreSQL 16 en Redis 7 uitgevoerd.

De resterende punten zijn bewust begrensde v1-contracten: historische
findings zonder expliciet window-/strategycontract blijven read-only. De
Plaid-like connector is alleen actief voor het expliciete
`transaction_history_gap`-contract; andere open-banking providers worden niet
automatisch aangenomen op basis van alleen een `fetch_transactions`-methode.
De backfill kan alleen expliciet contract-gekwalificeerde findings verwerken;
overige historische findings blijven read-only. De relevante architectuur-,
database-, observability- en migratiedocumentatie is bijgewerkt.

# Asynchronous Data Quality Remediation Pipeline

## Doel en uitgangspunt

Introduceer een persistente **Data Quality Remediation Backlog** waarmee
detectie en herstel strikt van elkaar gescheiden zijn:

```text
Detect -> Persist -> Prioritize -> Deduplicate -> Batch/Coalesce
       -> Rate-limit -> Remediate -> Verify
```

Een detector, reconciliation-run of import mag uitsluitend een issue lokaal
registreren. Alleen de remediation-executor mag een provider-call doen. De
eerste implementatie blijft binnen de bestaande modular monolith, PostgreSQL,
APScheduler en Redis-stack.

## Huidige architectuuranalyse

### Wat al bruikbaar is

- `src/finance_sync/services/reconciliation.py` detecteert duplicate
  transactions, cross-connector gaps en missing transaction windows en
  schrijft `ReconciliationRun`/`ReconciliationResult` records.
- `src/finance_sync/services/data_quality.py` geeft een tenant-scoped
  read-model voor deze findings.
- `src/finance_sync/services/data_quality_repair.py` bevat al veilige,
  idempotente maar synchrone repairlogica voor Trading212-instrumentmetadata
  en quote/historical-price enrichment.
- `src/finance_sync/api/v1/control_plane.py` exposeert
  `GET /control-plane/data-quality`, `GET /control-plane/data-health` en
  `POST /control-plane/data-health/repair`; die POST enqueue't sinds de
  verticale slice alleen nog backlogwerk en retourneert `202`.
- `src/finance_sync/worker/jobs.py` draait reconciliation, enrichment en de
  nieuwe bounded `data_quality_remediation_job`; `scheduler.py` registreert
  die job via de persistente APScheduler job store.
- `UnitOfWork` en `db/repositories.py` zijn de bestaande toegangspatronen voor
  tenant-scoped persistence. De claimlogica van `schedule_runner.py` en de
  bestaande outbox-claim migration zijn referenties voor concurrency.
- `connectors/base.py` biedt connector capabilities en een
  `RateLimitPolicy`; `connectors/rate_limiter.py` biedt lokale retry/backoff.
  Dit is niet voldoende voor meerdere workerprocessen: remediation moet de
  bestaande Redis-infrastructuur uitbreiden met een gedeelde reserveeractie.
- Alembic is de enige schema-owner. Releases gebruiken expand-first,
  backward-compatible migrations en image rollback zonder automatische
  downgrade.

### Huidige tekortkomingen die eerst moeten worden opgelost

- `ReconciliationResult` blijft per run opgeslagen en findings worden
  aanvullend stabiel gededupliceerd in de remediation backlog;
  `remediation_item_id` bewaart nu de relationele trace-link.
- `DataQualityRepairService.run()` bestaat nog als legacy/helper voor
  backwards-compatible callers; de periodieke worker voert deze globale
  directe repairloop niet meer uit. Identity-, historical-price- en
  latest-quote-remediation lopen via de backlog; verdere provider-adapters
  en het veilig verwijderen van deze helper blijven open.
- Connector `RateLimiter` is in-memory en per connector-instance. Het kan dus
  niet garanderen dat meerdere workerinstances samen een providerquota
  respecteren.
- Reconciliation maakt findings en registreert de expliciet ondersteunde
  strategy-contracten; overige finding-types hebben nog geen automatische
  remediation mapping.
- Missing-transaction findings worden bij finalize automatisch gemapt naar
  `transaction_history_gap` als de account een connection en de connector
  een expliciete capability heeft; de context bevat provider-account en
  begrensd analysevenster. Onbekende providers blijven `unsupported`.
- De historische reconciliation-backfill is capability-driven voor dezelfde
  expliciet gedeclareerde connectors en controleert tenant-account,
  connection en volledig window-contract; incomplete findings blijven
  read-only.
- De data-health UI toont nu enqueue-only repair en de status van de
  asynchrone herstelqueue; verdere provider-specifieke UI-adoptie blijft
  afhankelijk van production adapters.

## Voorgestelde architectuur

Voeg binnen het bestaande pakket een nieuwe capability toe:

```text
finance_sync/
  reconciliation/
    detectors/                 # pure/goedkope issue detection
    remediation/
      backlog.py                # repository + registration API
      planner.py                # due work selection, priority, coalescing
      executor.py               # enige laag met provider API-calls
      batching.py               # compatibility keys and batch formation
      policies.py               # strategy, retry, quota and provider policy
      verification.py           # re-run validator / resolve outcome
      providers.py              # provider remediation extension point
  models/remediation.py
  schemas/remediation.py
  services/remediation.py      # tenant-scoped application service
```

Gebruik de bestaande `services/reconciliation.py` voorlopig als orchestrator
en verplaats pas logica naar `reconciliation/detectors/` wanneer dat een
concrete test- of hergebruikvoordeel geeft. Bouw geen tweede sync-framework.

### Componentverantwoordelijkheden

- **Detector**: analyseert alleen PostgreSQL/canonical data en produceert
  `DetectedIssue`-waarden. Geen credential loading, connector construction,
  Redis reservation of provider-call.
- **Backlog repository**: atomair registeren/updaten, dedupliceren, claimen,
  leases verlengen, state transitions en tenant-scoped queries.
- **Remediation backlog**: durable truth voor werk en laatste status; niet
  vervangen door APScheduler of Redis.
- **Planner**: haalt due items op, past eenvoudige priority/aging toe, vraagt
  provider strategy op, groepeert compatibele items en reserveert quota. Geen
  provider API-call.
- **Executor**: laadt één connection, decrypt credentials vlak voor gebruik,
  voert één provider-specifieke batch uit en classificeert de uitkomst.
- **Verification**: gebruikt lokale data en de relevante detector/validator.
  Alleen verification kan een item `resolved` maken.
- **APScheduler-job**: dunne tick die planner/executor aanroept; geen
  per-item state in geheugen.

## Issue-registratie, deduplicatie en idempotency

Definieer een immutable `DetectedIssue` met ten minste:

```text
tenant_id, provider_key, connection_id, issue_type,
affected_entity_type, affected_entity_id, severity, priority,
remediation_strategy, context, detected_at
```

`connection_id` is onderdeel van de sleutel als hetzelfde provider-account in
meerdere connections kan voorkomen; anders blijft het `NULL`/provider-scope.
Normaliseer strings, UUID's, datums en batchvensters voordat de sleutel wordt
berekend. Bouw een stabiele `deduplication_key`, bijvoorbeeld:

```text
sha256("dq:v1|tenant=<id>|connection=<id-or->|provider=<key>"
       "|type=<issue>|entity=<type>:<id>|scope=<stable-scope>")
```

De scope bevat bij een gap een genormaliseerd dagvenster (`from/to`) of een
stabiele resource-identiteit, niet een run-id, beschrijving of volatile
payload. Een duplicate pair sorteert beide transaction IDs eerst. Laat de
database een unieke constraint afdwingen op `(tenant_id,
deduplication_key)`; een detector gebruikt `INSERT ... ON CONFLICT DO UPDATE`
of een savepoint/unique-conflict retry.

Bij een bestaande actieve finding wordt alleen bijgewerkt: `last_seen_at`,
`severity`/priority volgens een monotone policy, context (redacted merge),
`remediation_strategy` en eventueel `next_attempt_at` wanneer die nog niet
claimed is. `first_detected_at` blijft gelijk en `attempt_count` reset niet.

Bij een `resolved` item:

- een detectie vóór een configureerbare re-open grace period bevestigt alleen
  de finding en maakt hem niet opnieuw actief;
- een detectie ná die periode of met een nieuwe inhoudelijke scope maakt een
  nieuw issue met een nieuwe generation/sleutel;
- verificatie die de issue niet vindt mag niet oneindig direct opnieuw
  enqueue-en: markeer resolved en laat een latere onafhankelijke scan een
  nieuwe generatie openen.

Concurrente detectors zijn veilig door de unieke constraint en één atomische
upsert. Gebruik geen Redis als deduplicatiebron.

## Lifecycle/state machine

Gebruik alleen deze states in v1:

```text
pending -> claimed -> processing -> resolved
   |          |           |
   v          v           v
deferred   pending      retry_wait -> pending
                           |
                           +-> failed
                           +-> manual_review
pending/failed/manual_review -> (operator) pending of ignored
```

`claimed` en `processing` mogen als één persistente `processing`-status
worden geïmplementeerd met claim metadata; een aparte `claimed` state voegt
geen operatorwaarde toe. Gebruik dus in de tabel: `pending`, `processing`,
`deferred`, `retry_wait`, `resolved`, `failed`, `ignored`, `manual_review`.

- `pending`: uitvoerbaar zodra `next_attempt_at <= now`.
- `processing`: lease actief; niet opnieuw claimen vóór expiry.
- `deferred`: bewust uitgesteld, typisch ontbrekend quota/dependency; planner
  zet later terug naar `pending`.
- `retry_wait`: transient fout met concrete `next_attempt_at`.
- `resolved`: verification bevestigde dat het oorspronkelijke issue weg is.
- `failed`: terminale automatische fout, operator kan requeue-en.
- `manual_review`: niet automatisch veilig/ondersteund; blokkeert alleen dit
  issue, nooit andere batches.
- `ignored`: expliciet operatorbesluit, met auditgegevens.

## Database model en migratie

Voeg `models/remediation.py` toe met `DataQualityRemediationItem` en
registreer het model via `models/__init__.py`. Maak een nieuwe Alembic-revisie
na `0067` (concreet `0068_add_data_quality_remediation_backlog.py`). Gebruik
UUID-foreign keys naar `tenants`, optioneel `credentials` voor
`connection_id`, en `JSONB` alleen voor context/payload; query- en
securityvelden blijven typed.

Aanbevolen kolommen:

```text
id UUID PK
tenant_id UUID NOT NULL FK tenants ON DELETE CASCADE
provider_key VARCHAR(64) NOT NULL
connection_id UUID NULL FK credentials ON DELETE SET NULL
issue_type VARCHAR(64) NOT NULL
affected_entity_type VARCHAR(64) NOT NULL
affected_entity_id VARCHAR(256) NOT NULL
severity VARCHAR(16) NOT NULL
priority INTEGER NOT NULL DEFAULT 0
status VARCHAR(24) NOT NULL DEFAULT 'pending'
first_detected_at TIMESTAMPTZ NOT NULL
last_seen_at TIMESTAMPTZ NOT NULL
next_attempt_at TIMESTAMPTZ NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
rate_limit_deferral_count INTEGER NOT NULL DEFAULT 0
remediation_strategy VARCHAR(96) NOT NULL
deduplication_key VARCHAR(128) NOT NULL
batch_key VARCHAR(256) NULL
context JSONB NOT NULL DEFAULT '{}'
claim_token UUID NULL
claimed_at TIMESTAMPTZ NULL
lease_expires_at TIMESTAMPTZ NULL
last_error TEXT NULL                 # sanitized, bounded
last_error_category VARCHAR(32) NULL
resolved_at TIMESTAMPTZ NULL
verification_count INTEGER NOT NULL DEFAULT 0
created_at/updated_at TIMESTAMPTZ NOT NULL
```

Maak een unieke constraint op `(tenant_id, deduplication_key)`. Indexeer:

- `(status, next_attempt_at, priority DESC, first_detected_at)` voor due
  claiming;
- `(tenant_id, status, next_attempt_at)` voor API en tenant-isolatie;
- `(provider_key, status, next_attempt_at)` voor quota-pressure en planner;
- `lease_expires_at` voor stale-lease recovery;
- `(tenant_id, batch_key, status, next_attempt_at)` voor aggregation.

Gebruik een partial index waar PostgreSQL dat ondersteunt voor actieve statuses.
Bewaar geen secrets of volledige providerresponses in `context`/`last_error`;
redacteer en cap payloads. Retentie: resolved/ignored minimaal de bestaande
audit-/data-retentieperiode behouden, daarna archiveren/verwijderen via een
periodieke cleanup job; actieve en manual-review items nooit automatisch
verwijderen.

De migration is expand-only en backwards-compatible: eerst tabel/indexes,
dan code die dual-safe schrijft, daarna pas directe repair uitschakelen.
Bestaande reconciliation records worden niet blind naar backlog-items
gekopieerd; voeg een eenmalige, idempotente backfill toe alleen wanneer de
gekozen strategies de finding veilig kunnen mappen. Onbekende findings blijven
read-only zichtbaar.

## Claiming, leases en concurrency

De backlog repository voert in één PostgreSQL-transactie uit:

1. selecteer maximaal `planner_batch_size` rows met `status IN
   ('pending','retry_wait','deferred')`, `next_attempt_at <= now()`, of
   `processing` waarvan `lease_expires_at < now()`;
2. sorteer op score (zie scheduling) plus `first_detected_at`;
3. `FOR UPDATE SKIP LOCKED`;
4. update naar `processing`, zet een random `claim_token`, `claimed_at` en
   `lease_expires_at = now() + lease_duration`, en increment `attempt_count`
   alleen voor een echte execution claim;
5. commit vóór provider-I/O.

Executor updates gebruiken `(id, claim_token)` in de WHERE-clause. Een crash
laat een stale lease achter; de volgende planner mag die terugzetten naar
`pending`, met behoud van attempt/error history. Lange batches verlengen hun
lease periodiek, maar mogen nooit zonder bovengrens blijven draaien.

At-least-once uitvoering is het model: een crash ná provider-call en vóór
status-update kan duplicate execution veroorzaken. Provider strategies moeten
idempotente fetch/upsert-requests gebruiken en de executor moet geen lokale
mutatie als resolved markeren zonder verification. Geen claim wordt via Redis
gedaan; Redis blijft coordination/quota-hulp.

## Prioritization en scheduling

Start met een uitlegbare score, niet met FIFO:

```text
score = severity_weight + user_impact_weight + min(age_hours / 24, 30)
        - min(attempt_count, 10) - cost_weight
```

`priority` is een operator-/policy override die rechtstreeks wordt opgeteld.
Provider quota en dependency readiness zijn harde filters, geen reden om een
onuitvoerbaar item steeds hoger te zetten. Sorteer daarna op score en oudste
`first_detected_at`. Reserveer periodiek een klein fairness-aandeel voor de
oudste due items per provider/tenant, zodat lage prioriteit niet permanent
starvet. Later kunnen user impact, dependency graph en quota pressure als
aparte policy objecten worden toegevoegd.

## Provider abstraction

Voeg geen provider-branches toe aan de core executor. Definieer een
`RemediationStrategy` protocol/registry met bijvoorbeeld:

```python
class RemediationStrategy(Protocol):
    key: str
    def supports(self, issue: DetectedIssue) -> bool: ...
    def grouping_key(self, item: Item) -> str: ...
    def batch_limit(self, context: ProviderContext) -> int: ...
    def request_cost(self, batch: Batch) -> QuotaCost: ...
    async def execute(self, batch: Batch, connector: Connector) -> BatchResult: ...
    def classify_error(self, exc: Exception) -> ErrorClassification: ...
    def partial_results(self, response: object, batch: Batch) -> ItemOutcomes: ...
    async def verify(self, item: Item, session: AsyncSession) -> VerificationResult: ...
```

Laat provider connectors via een registry declareren welke strategies ze
ondersteunen, batch limits, endpoint-family quota scope en partial-failure
semantics. De eerste concrete strategies zijn gericht op bestaande code:

- Trading212 instrument metadata / unresolved security resolution, gebaseerd
  op `fetch_instruments()` en bestaande repaircode;
- transaction-history gap per connection/account/dagvenster, alleen voor
  providers die een historische fetch kunnen uitvoeren;
- historical price gap per security/interval, aansluitend op
  `EnrichmentGateway`.

Een provider zonder strategy leidt tot `manual_review`, niet tot een
ongecontroleerde generic API-call.

## Batching, work aggregation en request coalescing

De planner maakt per due set eerst een compatibility key:

```text
provider + connection + strategy + endpoint_family + auth_scope
        + currency/interval + request_shape + dependency_partition
```

Items met verschillende credentials, account scopes, intervals, endpoint
families, required date semantics of incompatible provider capabilities mogen
niet in één batch. Voor compatibele items:

1. normaliseer overlappende dagvensters en sorteer ranges;
2. coalesce aangrenzende transaction-dagen binnen een configureerbare
   `max_history_window_days`;
3. groepeer instrument IDs voor een batch metadata endpoint;
4. groepeer securities per price interval en provider batch endpoint;
5. splits op provider `batch_limit`, URL/body-limiet, max date span en
   `request_cost`.

Dit is **batch processing**, **work aggregation**, **request coalescing** en
**batch coalescing** tegelijk, maar blijft strategy-driven in plaats van een
perfect generiek framework te beloven.

Een batchresultaat bevat per item een outcome. Eén response kan dus meerdere
items oplossen. Bij partial success worden geslaagde items afzonderlijk naar
verification gebracht; mislukte items krijgen hun eigen classificatie en
backoff. Een response zonder ondubbelzinnige item mapping mag niets als
resolved markeren en gaat naar retry/manual review afhankelijk van oorzaak.
Meet expliciet `items_in_batch`, `provider_calls_saved` en gemiddeld batchformaat.

## Provider-aware rate limiting

Maak een `RemediationRateLimitCoordinator` bovenop Redis. De policy wordt
geïdentificeerd door `(provider, connection-or-tenant scope,
endpoint_family)` en bevat requests/window, concurrency, token/cost per
request, batch limits en cooldown. Gebruik een atomische Lua-script of
transactionele Redis-operatie om tokens/costs te reserveren, met TTL; alle
workerinstances gebruiken exact dezelfde keys. De bestaande connector policy
blijft de connector-default, maar remediation policies mogen endpoint-families
overschrijven.

Als quota niet beschikbaar is, is dat geen fout: release/geen reservation en
zet alle betrokken items op `deferred` met `next_attempt_at` op de vroegst
bekende refilltijd plus jitter. Een planner mag nooit wachten met een
PostgreSQL-lease open. Bij HTTP 429:

- parse en cap `Retry-After` (seconden of HTTP-date);
- update Redis cooldown voor de relevante scope;
- zet items op `deferred`, verhoog alleen
  `rate_limit_deferral_count`, niet het normale retry-budget;
- log status en provider scope, nooit response body/secrets.

Bij ontbrekende Redis-configuratie moet de feature fail closed voor provider
calls waarvoor gedeeld quota nodig is: items blijven deferred/manual review
volgens policy, tenzij een expliciete single-worker veilige policy bestaat.

## Retry en error classification

Leg één classificatie vast in `policies.py`:

| Klasse | State/actie |
|---|---|
| transient (timeout, 5xx, netwerk) | `retry_wait`, exponential backoff + jitter, max attempts |
| rate limit / 429 | `deferred`, Retry-After/cooldown; geen normaal retry-budget |
| provider validation / permanent 4xx | `failed` of `manual_review`, geen blind retry |
| authentication/credential expired | `manual_review`, connection health signal; niet per item blijven retryen |
| unsupported strategy/endpoint | `manual_review` |
| internal bug / schema mismatch | `failed`, alert en bounded diagnostic; geen provider storm |

Gebruik bestaande `TransientError`, `PermanentError` en `RateLimitError`, maar
maak provider adapters verantwoordelijk voor vertaling. Backoff is
`min(cap, base * 2**attempt) * uniform(1-jitter, 1+jitter)`, met max attempts
per strategy. Persist de berekende `next_attempt_at`; APScheduler hoeft alleen
te pollen.

## Verification en loop prevention

Na een succesvolle provider-call:

1. commit opgehaalde canonical data/upserts in een korte transactionele
   boundary;
2. voer de relevante lokale validator/reconciliation-check uit;
3. markeer alleen `resolved` als het oorspronkelijke dedup/scope-issue niet
   meer bestaat;
4. anders zet op `retry_wait` (als data nog async moet materialiseren) of
   `manual_review` na de configured verification attempts.

Verification is idempotent en tenant-scoped. Bewaar verification count,
laatste resultaat en timestamp in context/typed metadata. Een detector die
hetzelfde probleem direct weer ziet, mag een resolved item niet eindeloos
heropenen; gebruik de generation/grace policy uit de deduplicatiesectie.

## Worker-integratie

Vervang de huidige globale provider-call in `data_quality_repair_job` door een
dunne `data_quality_remediation_job` die per tick beperkte due batches plant
en uitvoert. Voeg instellingen toe voor enabled, poll interval, claim limit,
lease duration, max execution time, retry/backoff caps, verification attempts,
retentie en per-provider policy. Gebruik dezelfde APScheduler registratie en
`JobRunContext`; voorkom een tweede scheduler.

Reconciliation finalize doet na het opslaan van findings een goedkope
`BacklogService.register_detected_issues(...)` in dezelfde PostgreSQL
transaction (of een expliciete outbox als de transaction boundaries dit
vereisen). Detectorcode importeert geen executor. De bestaande directe repair
endpoint wordt backward-compatible eerst veranderd naar enqueue en retourneert
`202`/summary met item IDs; bestaande clients krijgen geen provider-call meer.

## API/UI en operationele bediening

Breid `api/v1/control_plane.py` uit met tenant-scoped endpoints:

- `GET /control-plane/remediation` met pagination en filters provider,
  status, issue type, severity en connection;
- `GET /control-plane/remediation/{id}` met context, attempts, lease en laatste
  fout (redacted);
- `POST /control-plane/remediation/{id}/retry` en `/requeue`;
- `POST /control-plane/remediation/{id}/ignore` met reden;
- `PATCH /control-plane/remediation/{id}` voor beperkte priority/manual-review
  wijzigingen;
- optioneel `POST /control-plane/remediation/bulk-retry` met bounded IDs.

Alle mutaties vereisen passende `reconciliation`/`enrichment`-write
permissions, schrijven bestaande audit trail waar beschikbaar en controleren
tenant ownership vóór object lookup. Geen endpoint accepteert vrije provider
URLs, credentials of arbitrary strategy keys.

Voeg in `schemas/remediation.py` response/request DTO's toe. Breid dashboard
data-health uit met backlog count, oudste item, status per provider, retry/
manual-review acties en rate-limit pressure. De eerste UI hoeft geen volledige
batchvisualisatie te tonen; API en operatorstatus zijn voldoende.

## Observability

Registreer Prometheus metrics volgens bestaande
`observability/monitoring`-patronen:

- `data_quality_remediation_backlog_size{status}`;
- `data_quality_remediation_oldest_age_seconds`;
- `..._pending_by_provider{provider}`;
- `..._throughput_total{provider,strategy,outcome}`;
- `..._deferred_rate_limit_total{provider,endpoint_family}`;
- `..._attempts_total` en success/failure counters;
- `..._manual_review_size`;
- `..._batches_created_total`, `..._batch_items_total`;
- `..._average_items_per_batch`;
- `..._api_calls_saved_total`.

Structured logs bevatten `tenant_id` alleen waar veilig voor logs,
`backlog_item_id`, provider, connection_id, strategy, batch_id, batch_size,
claim_token-hash, attempt, classification, next_attempt_at en saved-calls.
Log geen credentials, raw provider payloads, tokens of onbeperkte error text.
Voeg alerts toe voor oldest pending/manual-review, sustained failed rate,
rate-limit deferral pressure en lease recovery spikes. Health/metrics moeten
een grote backlog niet per item laden.

## Security en tenant isolation

Alle repository queries bevatten tenant scope; API filters mogen die scope
nooit vervangen. `tenant_id` in een detector-input wordt server-side bepaald,
niet uit vrije clientdata vertrouwd. `connection_id`, account/entity IDs en
JSONB context worden gecontroleerd tegen dezelfde tenant. Credential decryptie
gebeurt alleen in executor vlak voor call; niets wordt in backlog, batch key,
metrics of logs opgeslagen. Operator ignore/retry/requeue acties worden
geaudit. Een cross-tenant unieke key is niet voldoende als enige bescherming:
foreign keys, service checks en API dependencies blijven nodig.

## Concrete bestanden/modules

### Bestaand aanpassen

- `src/finance_sync/services/reconciliation.py`: expose detector findings en
  registreer eligible issues na finalize, zonder provider-call.
- `src/finance_sync/services/data_quality_repair.py`: splits bestaande veilige
  strategieën uit naar provider remediation adapters; behoud alleen pure
  mapping/repair helpers waar nuttig.
- `src/finance_sync/worker/jobs.py`: vervang directe repairjob door backlog
  planner/executor tick; behoud nightly reconciliation als detector/sync-job.
- `src/finance_sync/worker/scheduler.py`: registreer nieuwe job met bestaande
  APScheduler conventions.
- `src/finance_sync/connectors/base.py`, `connectors/capabilities.py` en
  `connectors/registry.py`: voeg optionele remediation strategy metadata toe,
  backwards-compatible voor bestaande connectors.
- `src/finance_sync/connectors/rate_limiter.py`: hergebruik policy/backoff-
  waarden, maar plaats gedeelde quota-coördinatie in remediation zodat de
  bestaande sync-semantiek niet breekt.
- `src/finance_sync/db/uow.py`, `db/repositories.py`, `models/__init__.py`:
  model/repository wiring.
- `src/finance_sync/api/v1/control_plane.py`, `api/v1/router.py` en
  `schemas/data_quality.py`/nieuwe `schemas/remediation.py`.
- `src/finance_sync/templates/dashboard.html`: backlog- en operatorstatus.
- `src/finance_sync/config/settings.py`: feature flags, polling, leases,
  backoff, verification en retention.
- `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/observability.md` en
  `docs/MIGRATIONS.md` voor de nieuwe contracten.

### Nieuw

- `src/finance_sync/models/remediation.py`;
- `src/finance_sync/reconciliation/remediation/{backlog,planner,executor,batching,policies,verification,providers}.py`;
- `src/finance_sync/schemas/remediation.py`;
- `migrations/versions/0068_add_data_quality_remediation_backlog.py` en
  `0069_link_reconciliation_to_remediation.py`;
- provider strategy modules, bij voorkeur onder
  `src/finance_sync/connectors/remediation/` of een registry-owned adapter;
- unit/integratietests in `tests/` volgens de bestaande async SQLAlchemy- en
  fake connector patterns.

## Gefaseerde delivery strategy

### Phase 1 — Durable backlog en detector decoupling

Model, migration, repository, unique dedup upsert en strategy-agnostische
registratie. Reconciliation findings worden automatisch geregistreerd zonder
provider-call. Direct repair endpoint enqueue’t alleen.

Acceptance criteria:

- duplicate scans produceren één item per stabiele sleutel;
- concurrent inserts eindigen in één row zonder exception naar de detector;
- alle vereiste backlogvelden en tenant constraints bestaan;
- geen detectorpad importeert of aanroept een connector executor/provider.

### Phase 2 — Basic planner/executor, claim/lease en retry

Implement processing state, `FOR UPDATE SKIP LOCKED`, claim token/lease,
stale recovery, bounded execution en transient/permanent classification.
Adapteer eerst Trading212 metadata als verticale slice en voeg verification
toe voor die strategy.

Acceptance criteria:

- twee workers claimen nooit dezelfde actuele lease;
- crash/stale lease wordt opnieuw uitvoerbaar;
- replay is idempotent en verification beslist over `resolved`;
- transient errors gebruiken jittered exponential backoff.

### Phase 3 — Provider-aware Redis rate limiting

Implementeer gedeelde reservation/cooldown, endpoint-family policy,
Retry-After en deferred scheduling. Voeg metrics voor quota pressure toe.

Acceptance criteria:

- meerdere workers blijven onder een testquota;
- ontbrekend budget deferred is, geen failure;
- 429 met Retry-After plant op minstens die delay en verbruikt geen normaal
  retry-budget.

### Phase 4 — Batching, work aggregation en request coalescing

Implementeer grouping/compatibility keys, range merge, provider batch limits,
partial outcomes en call-saved metrics. Voeg transaction history en/of price
history strategy alleen toe waar de actuele connector abstractions dit veilig
ondersteunen.

Acceptance criteria:

- meerdere dagen/account worden één request waar compatibel;
- incompatibele credentials/endpoints worden gesplitst;
- één response kan meerdere backlog-items oplossen;
- partial batch success retry’t alleen mislukte items.

### Phase 5 — Verification lifecycle en loop prevention

Maak relevante validators herbruikbaar, voeg verification counters/results toe,
resolve/manual-review transitions en generation/grace behavior.

Acceptance criteria:

- succesvolle fetch zonder verdwenen issue wordt niet `resolved`;
- een issue dat na verification blijft bestaan loopt niet onbeperkt;
- resolved issues blijven zichtbaar maar worden niet door elke scan gedupliceerd.

### Phase 6 — API/UI, observability en manual operations

List/detail/filter, retry/requeue/ignore/priority, dashboardstatus, metrics,
structured logs en alerts. Voeg retention cleanup toe.

Acceptance criteria:

- operator kan oldest/manual-review items vinden en één/bulk requeue uitvoeren;
- cross-tenant IDs leveren 404/forbidden volgens bestaande API-conventie;
- backlog metrics en batch efficiency zijn scrape- en dashboardbaar.

### Phase 7 — Provider adoption

Migreer overige veilige repairs uit `DataQualityRepairService`, voeg provider
strategies toe via extension point en retire de directe globale repairloop.
Elke provider krijgt contract-, quota-, batching-, partial-failure- en
verification-tests voordat de feature flag default-on wordt.

## Teststrategie

Voeg minimaal tests toe voor:

- duplicate issue detection, stable key en resolved re-detection;
- concurrent inserts/upsert race en tenant isolation;
- concurrent workers, `SKIP LOCKED`, claim token en stale lease recovery;
- worker crash tijdens processing en idempotent replay;
- rate-limit deferral, ontbrekend budget, Redis cooldown en HTTP 429/
  `Retry-After`;
- exponential backoff met gecontroleerde jitter en max attempts;
- batch compatibility, range coalescing en max batch split;
- partial batch failure en één response die meerdere items oplost;
- successful remediation maar failed verification;
- manual-review/failed/ignore/requeue transitions;
- API filters, permissions en cross-tenant access;
- migration upgrade/rollback-syntax en indexes/unique constraint;
- scheduler registration en job monitoring;
- metrics/log redaction en geen provider-call vanuit detector.

Gebruik unit-tests voor pure scoring, keys, grouping en classification, async
repository tests met PostgreSQL-compatibele fixtures voor locking/constraints,
en fake connectors voor provider-call counts. Test echte Redis semantics in
een integration test; een in-memory fake mag alleen policy-unit tests dragen.

## Non-goals v1

- geen distributed workflow engine, Kafka, event sourcing of generic BPM;
- geen Celery uitsluitend voor deze capability;
- geen machine-learning prioritization;
- geen perfect generiek batching-framework voor iedere toekomstige provider;
- geen automatische correctie van financiële feiten (duplicates verwijderen,
  cost basis verzinnen of transfers reconstrueren zonder expliciete strategy);
- geen provider-call in een API request of detector transaction;
- geen globale cross-tenant backlog-operator zonder bestaande admin/audit
  controls.

## Risks en open questions

- Welke bestaande transaction-history connectors kunnen betrouwbaar een
  willekeurig dagvenster ophalen zonder sync-cursorsemantiek te breken?
- Welke provider policies zijn werkelijk endpoint-family-specific en welke
  quota scope is connection-, tenant- of account-level?
- Kan de huidige EnrichmentGateway een batch price-history contract bieden,
  of moet Phase 4 eerst alleen metadata doen?
- Welke retentiontermijn geldt voor remediation context/audit versus
  reconciliation findings?
- Moet een `ReconciliationResult` later verwijzen naar backlog-item ID voor
  UI-traceability, of volstaat de deduplication key?
- Welke Redis deployment/atomic-script rollout en failure policy is vereist?
- Hoe wordt credential-expiry gedeeld tussen remediation en de bestaande
  provider-health/sync-run diagnosis?

## Definition of done

De capability is klaar wanneer detectie provider-onafhankelijk en goedkoop
issues registreert, PostgreSQL de durable backlog beheert, meerdere workers
veilig claimen met leases, de planner compatibele work coalescet, Redis quota
globaal respecteert, de executor enablers provider-calls uitvoeren,
verification resolution bevestigt, operators failures kunnen herstellen en de
volledige flow met de hierboven genoemde tests en metrics aantoonbaar werkt.
