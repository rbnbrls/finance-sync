---
title: "Volledige lifecycle voor providers en connectoren"
status: done
priority: 20
---

# Implementatieplan: volledige provider- en connectorlifecycle

## Doel

Maak provider health en connectorbeheer begrijpelijk en uitvoerbaar voor
gebruikers en operators. Een verbinding is pas gezond wanneer drie
onafhankelijke niveaus zichtbaar zijn:

1. **Verbinding** — credentials, provider-authenticatie en bereikbaarheid;
2. **Brondata** — per resource (accounts, transactions, holdings enzovoort)
   is de bron beschikbaar en zijn de laatste resultaten valide;
3. **Laatste succesvolle verwerking** — de laatste sync per resource is
   succesvol, niet te oud en niet geblokkeerd door een connectorprobleem.

`connected` mag dus nooit uitsluitend betekenen dat credentials bestaan of
dat één authenticatiecall geslaagd is.

Dit plan bouwt voort op de bestaande entry-point registry, `ConnectorHealth`,
`Credential`, `SyncRun`, `SyncCursor`, `ConnectorState`, connector lifecycle
configuratie en het control-plane/data-health scherm. Bestaande
connectoren, opgeslagen data en API-clients moeten backward-compatible blijven.

## Bestaande basis en expliciete gaten

Al aanwezig:

- connector discovery via de `finance_sync.connectors` entry-point group;
- connector- en SDK-versie metadata in `ConnectorRegistry`;
- `supported_resources` en `RateLimitPolicy`;
- gesanitiseerde connection-test- en sync-foutmetadata;
- per-connection `last_attempt_at`, `last_success_at`, `last_error` en
  pause/resume;
- `config/connector-lifecycle.json` en `scripts/connector_lifecycle.py`;
- contracttests, fixtures en migration expand/contract beleid.

Nog nodig:

- een productcatalogus die statische metadata en runtime-status combineert;
- formele compatibiliteits- en migratiestatus per geïnstalleerde connector;
- health per connection én per resource, met freshness en laatste verwerking;
- begrijpelijke rate-limitdiagnose met retry-tijd en impact;
- reauthenticatie en token-expiry als afzonderlijke lifecycle-acties;
- gecontroleerde connectorversie-updates met validatie, rollout en rollback;
- UI/API-contracten en audit/evidence voor deze lifecycle-acties.

## Uitvoeringsvolgorde

Voer de stories in deze volgorde uit. Elke story blijft afzonderlijk
mergebaar en moet zijn eigen tests toevoegen.

| Wave | Stories | Resultaat |
|---|---|---|
| 1 | FS-PL-01, FS-PL-02 | Catalogus, versie en compatibiliteitscontract |
| 2 | FS-PL-03, FS-PL-04 | Eén canoniek healthmodel en bruikbare diagnoses |
| 3 | FS-PL-05, FS-PL-06 | Credential- en update-lifecycle |
| 4 | FS-PL-07, FS-PL-08 | Gebruikersinterface, audit, documentatie en releasebewijs |

---

## FS-PL-01 — Ingebouwde connectorcatalogus met versie- en capabilitymetadata — gereed

**Implementatiestatus:** gereed op 2026-08-26. De registry levert nu
secret-veilige catalogusmetadata en `GET /api/v1/connectors/catalog` is
beschikbaar. Alle stories in dit plan zijn inmiddels geïmplementeerd en
gevalideerd.

### User story

Als gebruiker wil ik een catalogus van beschikbare providers zien, zodat ik
kan begrijpen wat een connector ondersteunt, welke versie actief is en wat ik
moet configureren voordat ik een connection toevoeg.

### Implementatie

- Breid de registry metadata uit met een stabiel schema voor:
  `provider_key`, `display_name`, `plugin_package`, `plugin_version`,
  `sdk_version`, `supported_resources`, `credential_schema`, `option_schema`,
  `rate_limit_policy`, `auth_mode`, `documentation_url`,
  `lifecycle_status` en `feature_flag`.
- Gebruik connector class metadata als primaire bron en voeg alleen
  catalogusvelden toe die niet veilig uit de class kunnen worden afgeleid.
  Voeg hiervoor een optionele `catalog_metadata` class attribute toe aan de
  base class; bestaande third-party connectoren blijven geldig met defaults.
- Maak `GET /api/v1/connectors/catalog` voor de tenant-geautoriseerde catalogus.
  Geef geen credentialwaarden, endpoint secrets of stacktraces terug.
- Houd `GET /api/v1/connectors` backward-compatible; voeg eventueel een
  `include_lifecycle=true` queryparameter toe in plaats van bestaande velden
  te hernoemen.

### Acceptatiecriteria

- [x] Elke geregistreerde connector verschijnt precies één keer met
  provider key, display name, actieve plugin/SDK-versie en capabilities.
- [x] De catalogus maakt duidelijk of een connector user-managed,
  file-based of staging-only is.
- [x] Rate-limitbeleid toont limiet, venster en retrybeleid, maar nooit
  credentials.
- [x] Een fout bij één optionele catalogusmetadata-entry maakt de API niet
  onbruikbaar; de entry krijgt `metadata_incomplete`.
- [x] Unit- en API-tests dekken built-in, third-party, duplicate-key en
  malformed-metadata gevallen.
- [x] OpenAPI wordt bijgewerkt en de bestaande connector-API-tests blijven
  groen.

## FS-PL-02 — Compatibiliteit, certificering en migratiewaarschuwingen

**Implementatiestatus:** gereed op 2026-08-26. De gedeelde
`connector_compatibility`-service wordt gebruikt door de catalogus-API, de
lifecycle-CLI en de sync-orchestrator. Versie-, capability-, fixture-,
certificerings-, feature-flag- en deprecationregels leveren vaste, veilige
status- en reason codes. Incompatibele connectoren worden vóór
authenticatie geblokkeerd; bestaande data blijft leesbaar.

### User story

Als gebruiker of operator wil ik weten of de geïnstalleerde connectorversie
compatibel is met mijn bestaande connection en data, zodat een update geen
stille sync-breuk veroorzaakt.

### Implementatie

- Maak één compatibiliteitsservice die `config/connector-lifecycle.json`,
  registry metadata, SDK-versie, capabilities, fixtureminimum en
  certificeringsstatus evalueert.
- Normaliseer statussen naar:
  `compatible`, `attention_required`, `deprecated`, `incompatible`,
  `disabled` en `unavailable`.
- Leg per connector vast: `current_version`, `previous_version`,
  `minimum_fixture_version`, `certification_status`, `certified_at`,
  `certification_commit`, `deprecation_date`, `removal_date` en
  `migration_required`.
- Voeg per opgeslagen connection een compatibiliteitsprojectie toe. Een
  connection die op een oudere versie is aangemaakt blijft leesbaar en
  synchroniseert alleen wanneer de versie compatibel is.
- Voeg waarschuwingen toe voor naderende deprecation, verwijderdatum,
  ontbrekende certificering, capabilityverlies en een vereiste reauth.
  Waarschuwingen zijn idempotent en mogen niet elke health-poll opnieuw als
  nieuwe auditactie worden opgeslagen.

### Acceptatiecriteria

- [x] De compatibiliteitsservice geeft dezelfde uitkomst voor API, worker,
  CLI-diagnose en UI.
- [x] Een ontbrekende capability of ongeldige SemVer veroorzaakt
  `incompatible` en blokkeert nieuwe syncs; bestaande data blijft leesbaar.
- [x] `deprecation_date` toont een waarschuwing vóór de datum en `removal_date`
  wordt nooit als gewone healthy-status gepresenteerd.
- [x] De response bevat connectorversie, rollbackversie, certificeringsdatum
  en een veilige reason code.
- [x] Tests dekken fixture te oud, verlopen certificering, feature flag uit,
  capabilityverlies, datumgrenzen en legacy connections.
- [x] De lifecycle-CLI blijft credentials en financiële data redigeren.

## FS-PL-03 — Canoniek provider-healthmodel met drie niveaus

**Implementatiestatus:** gereed op 2026-08-26. Het typed
`ProviderHealthOverview`-contract en `ProviderHealthService` projecteren
connection-, resource- en verwerkingsstatus tenant-scoped. Het control-plane
endpoint `GET /api/v1/control-plane/provider-health` gebruikt bestaande
credentials-, sync-run-, registry- en compatibiliteitsmetadata zonder
providercalls of credentialdecryption.

### User story

Als gebruiker wil ik in één overzicht zien of mijn verbinding, brondata en
laatste verwerking gezond zijn, zodat `Connected` geen misleidende status is.

### Implementatie

- Voeg een canoniek responsemodel toe, bijvoorbeeld
  `ProviderHealthOverview`, met:
  `overall_status`, `connection`, `resources`, `last_successful_processing`,
  `compatibility`, `action_required` en `evaluated_at`.
- `connection` bevat minimaal `status`, `checked_at`, `auth_status`,
  `credential_status`, `latency_ms`, `error_code` en gesanitiseerde `message`.
- Elk item in `resources` bevat `resource`, `supported`, `source_status`,
  `last_attempt_at`, `last_success_at`, `fresh_until`, `items_processed`,
  `sync_run_id`, `error_category` en `stale`.
- Definieer deterministische precedence: `incompatible`/`reauth_required`,
  daarna `unavailable`/`rate_limited`, daarna `stale`/`attention_required`,
  daarna `healthy`. Een unsupported resource is informatief en verlaagt
  overall health niet.
- Hergebruik `DataHealthService` voor de aggregatie, maar voorkom dat
  data-health en connection-health verschillende statusregels hanteren.

### Acceptatiecriteria

- [x] Credentials aanwezig + authenticatie geslaagd + nooit succesvol
  gesynchroniseerd resulteert niet in overall `healthy` maar in
  `attention_required` met actie `run_sync`.
- [x] Authenticatie geslaagd + transactions stale resulteert in een zichtbare
  resourcewaarschuwing, zonder gezonde holdings als ongezond te labelen.
- [x] Een mislukte accounts-fetch maakt alleen accounts ongezond; sibling
  resources blijven afzonderlijk zichtbaar.
- [x] Een provider outage verwijdert geen eerder geldige brondata en maakt de
  laatst bekende sync-historie zichtbaar.
- [x] Unit-, PostgreSQL-integratie- en contracttests dekken statusprecedence,
  tenant-isolatie en lege/legacy data.

## FS-PL-04 — Resource health, rate-limitdiagnose en herstelacties

**Implementatiestatus:** gereed op 2026-08-26. Rate-limitdiagnose is
persisted op credentials en sync runs met alleen veilige metadata
(`limited_at`, `retry_after_at`, `rate_limit_attempts`, `rate_limit_scope` en
`last_http_status`). De rate limiter respecteert `Retry-After`; de
orchestrator en de handmatige sync-endpoint blokkeren retries tijdens een
actieve, persistente backoff en retourneren de bestaande diagnose met een
409-actie-status. Provider health toont de diagnose naast de afzonderlijke
resource-health. Migratie `0042_add_rate_limit_diagnosis` maakt de velden
beschikbaar zonder bestaande cursors of brondata te wijzigen.

### User story

Als gebruiker wil ik weten welke bron precies faalt en of een rate limit de
oorzaak is, zodat ik gericht kan wachten, herauthenticeren of opnieuw syncen.

### Implementatie

- Breid connector- en sync-resultaatmodellen uit met een stabiele foutindeling:
  `authentication`, `token_expired`, `provider_unavailable`,
  `rate_limited`, `timeout`, `validation`, `incompatible`, `cancelled` en
  `unknown`.
- Bewaar rate-limitmetadata per connection/resource: `limited_at`,
  `retry_after_at`, `attempt_count`, `limit_scope` (provider/connection/
  resource) en `last_http_status`; nooit response bodies of tokens.
- Laat de rate limiter `Retry-After` respecteren en voorkom nieuwe requests
  vóór `retry_after_at`, ook na worker-restart (persistente state of een
  gedeelde Redis-key met TTL).
- Voeg veilige herstelacties toe: `retry_now` wanneer toegestaan,
  `run_sync`, `reauthenticate` en `view_diagnosis`. Een retry tijdens een
  actieve backoff geeft een duidelijke 409/actie-status terug.
- Toon freshness per resource op basis van configureerbare policy met een
  expliciete default; verberg niet dat een resource nooit succesvol verwerkt
  is.

### Acceptatiecriteria

- [x] Een 429 toont retry-tijdstip, scope en aantal retries, maar geen
  providerpayload.
- [x] Handmatige retry vóór de backoff wijzigt niets aan de provider en
  retourneert de bestaande diagnose.
- [x] Rate-limits voor connection A blokkeren connection B niet tenzij de
  policy expliciet provider-breed is.
- [x] Een failed sync advanced de `SyncCursor` niet; een geslaagde sync
  werkt resource-health en cursor atomair bij.
- [x] Tests dekken 429 met en zonder `Retry-After`, worker restart, parallelle
  connections, resource-isolatie en idempotente retry.

Verificatie: `126 passed` in de gerichte rate-limiter/provider-health/control-
plane/orchestrator/API-suite; aanvullende foutclassificatiechecks geslaagd; Ruff
en Pyright rapporteerden geen fouten. De persistente credential-guard dekt
worker-restart en connection-isolatie; resource-isolatie en cursor-atomiciteit
zijn getest via de bestaande provider-health en orchestrator-pipeline-tests.

## FS-PL-05 — Reauthenticatie en tokenverval als first-class lifecycle

**Implementatiestatus:** gereed op 2026-08-26. Credential lifecyclevelden,
expiryprojectie en de veilige reauthenticatieflow zijn toegevoegd. De nieuwe
endpoint test nieuwe credentials eerst via de optionele connector-hook en
schrijft pas daarna de encryptiepayload atomair weg. Mislukte reauthenticatie
laat de bestaande payload, accounts, cursors en connector state intact.
Legacy connectors blijven werken via de `authenticate()`-fallback; connectors
zonder expiry-hook rapporteren een onbekende vervaldatum. Migratie
`0043_add_credential_lifecycle` gebruikt veilige `unknown`/`1` defaults.

### User story

Als gebruiker wil ik weten wanneer mijn token verlopen of ingetrokken is en
de connection opnieuw kunnen autoriseren zonder de bestaande brondata kwijt
te raken.

### Implementatie

- Voeg credential lifecyclevelden toe (expand migration):
  `credential_status`, `last_authenticated_at`, `expires_at`,
  `reauth_required_at`, `last_auth_error_code` en `credential_version`.
  Bestaande rijen krijgen veilige `unknown` defaults.
- Breid de connector SDK optioneel uit met `credential_expiry()` en
  `reauthenticate()`; connectors zonder deze hooks vallen terug op
  `authenticate()` en rapporteren `expiry_unknown`.
- Voeg `POST /api/v1/connectors/{connection_id}/reauthenticate` toe.
  De endpoint accepteert alleen nieuwe credentials/options volgens het
  bestaande encryptiepad, test eerst, commit daarna en laat geselecteerde
  accounts, cursors en historie intact.
- Classificeer 401/403 en provider-specifieke expiry-signalen als
  `reauth_required` wanneer dat aantoonbaar is; anders `authentication`.
- Maak expirywaarschuwingen tijdgebaseerd (bijvoorbeeld 7 en 1 dag),
  configureerbaar en timezone-onafhankelijk (UTC).

### Acceptatiecriteria

- [x] Verlopen of ingetrokken tokens tonen `reauth_required`, niet alleen
  `unhealthy` of `connected=false`.
- [x] Een mislukte reauthenticatie overschrijft de werkende credentials niet.
- [x] Een geslaagde reauthenticatie bewaart geen plaintext credential in
  response, logs, auditdetails, fixtures of exceptions.
- [x] Oude data, accountselectie, sync cursor en connector state blijven na
  reauthenticatie behouden.
- [x] Tests dekken token expiry, ingetrokken token, ontbrekende expiry,
  atomiciteit, tenant/permissie-isolatie en redaction.

Verificatie: 77 gerichte connector-, provider-health-, OpenAPI- en
multi-connectiontests geslaagd. Ruff, Pyright en `git diff --check` zijn
groen. De SDK-fallback, expirywaarschuwing, veilige foutclassificatie en
reauth-route zijn contractueel gevalideerd; bestaande encryptie- en
tenant-scopingtests blijven groen.

## FS-PL-06 — Gecontroleerde connectorupdates en rollback

**Implementatiestatus:** gereed op 2026-08-26. Er is een versioned
`ConnectorRelease`-record en een idempotente release-service toegevoegd voor
candidate-registratie, certificerings-/compatibiliteits-/canary-gates, promotion,
pause, resume en rollback. Providerbrede releaseacties zijn uitsluitend voor
menselijke administrators beschikbaar en worden via de bestaande auditlog
vastgelegd. De vorige release blijft behouden; rollback wijzigt alleen de
release-status en laat financiële data, cursors en auditgeschiedenis intact.
Migratie `0044_add_connector_releases` maakt de state machine persistent.

### User story

Als operator wil ik connectorupdates gecontroleerd uitrollen en kunnen
terugdraaien, zodat een nieuwe pluginversie geen bestaande connections of
syncdata beschadigt.

### Implementatie

- Maak een versioned connector release record met
  `provider_key`, `version`, `status` (`candidate`, `certified`, `enabled`,
  `deprecated`, `blocked`, `rolled_back`), `previous_version`,
  `certification_commit`, `enabled_at`, `disabled_at` en reason code.
- Definieer updateflow: discover → validate metadata → run contract/fixture
  checks → compatibility check → canary connection(s) → enable → monitor.
  Elke fase is idempotent en auditbaar.
- Houd de vorige versie beschikbaar zolang de nieuwe versie niet is
  gecertificeerd en bestaande connections de canary-check niet passeren.
- Blokkeer promotion bij ontbrekende certificering, incompatibele SDK,
  capabilityverlies zonder migratie of regressie in contracttests.
- Voeg operator-only endpoints/CLI-acties toe voor `promote`, `pause`,
  `rollback` en `resume`; gebruikers kunnen alleen hun connection op een
  door de operator vrijgegeven versie testen.
- Rollback beïnvloedt alleen connectorcode/configuratie; financiële data,
  cursors en audit history worden niet verwijderd of herschreven.

### Acceptatiecriteria

- [x] Een candidate connector kan niet automatisch actief worden zonder
  certificerings- en compatibiliteitsbewijs.
- [x] Promotion is atomair per provider en herhaalbaar na een worker restart.
- [x] Rollback naar `previous_version` is mogelijk wanneer de vorige versie
  beschikbaar is en schrijft een audit event.
- [x] Een connection die door een update incompatible is geworden wordt
  geblokkeerd met een migratieactie; bestaande data blijft leesbaar.
- [x] Tests dekken promotion gates, canary failure, rollback, concurrente
  operatoracties, legacy connections en auditredaction.

Verificatie: 42 release-state/API/OpenAPI- en connector-lifecycle-regressietests
geslaagd, inclusief certification-, compatibility- en canary-failure gates.
Ruff, Pyright en `git diff --check` zijn groen.

## FS-PL-07 — API en dashboard voor lifecycle-overzicht en acties

**Implementatiestatus:** gereed op 2026-08-26. De tenant-scoped endpoint
`GET /api/v1/connectors/{connection_id}/health` projecteert health read-only en
ondersteunt met `refresh=true` uitsluitend de lichte connector-healthcheck.
Het dashboard toont connection-, resource- en processing-health per connection,
naast versie/compatibiliteit, expiry, rate-limitdiagnose en concrete acties.
Alle status- en foutweergaven blijven secret-safe; acties behouden de
bestaande permission guards en verversen de geselecteerde connection.

### User story

Als gebruiker wil ik per connection de actuele lifecycle-status, diagnose en
volgende actie kunnen zien en uitvoeren vanuit het dashboard.

### Implementatie

- Voeg een connection-health endpoint toe:
  `GET /api/v1/connectors/{connection_id}/health`, met optionele
  `refresh=true` voor een expliciete lichte providercheck. Een gewone GET
  mag geen dure volledige sync uitvoeren.
- Voeg lifecyclevelden toe aan de bestaande connection response zonder
  bestaande velden te verwijderen. Voeg resource-health links en
  action IDs toe in plaats van frontendlogica aan reason strings te koppelen.
- Breid het dashboard uit met duidelijke badges/tekst voor:
  verbinding, brondata per resource, laatste succesvolle verwerking,
  connectorversie, compatibiliteit, tokenverval, rate-limit retry-tijd en
  aanbevolen actie.
- Voeg bevestiging toe voor riskante acties (reauth, purge, rollback) en
  loading/success/error/forbidden/409 states. Een refresh moet de geselecteerde
  connection en deeplink behouden.
- Gebruik bestaande permission guards: connection owners met connector/sync
  rechten mogen hun connection beheren; version promotion/rollback is
  operator-only.

### Acceptatiecriteria

- [x] De gebruiker kan niet alleen `Connected` zien; alle drie healthniveaus
  zijn afzonderlijk zichtbaar.
- [x] Per resource is zichtbaar: supported/unsupported, laatste poging,
  laatste succes, freshness/stale en foutcategorie.
- [x] Rate-limit en reauth acties tonen een concrete volgende stap en
  veranderen na succesvolle actie naar de nieuwe status.
- [x] Secrets, tokens, encrypted payloads, headers en stacktraces verschijnen
  nergens in HTML, JSON, logs of auditdetails.
- [x] GUI-, OpenAPI- en browser/E2E-tests dekken empty, loading, stale,
  failed, reauth-required, rate-limited en healthy scenarios.

Verificatie: 89 API/provider-health/control-plane/dashboardtests geslaagd,
OpenAPI-routecontract gecontroleerd, dashboard-JavaScript syntactisch
gevalideerd met Node.js, Ruff/Pyright en `git diff --check` groen.

## FS-PL-08 — Audit, operationele metrics, documentatie en release gate

**Implementatiestatus:** gereed op 2026-08-26. Lifecycle-mutaties hebben
expliciete tenant-scoped audit-events voor candidate-registratie, promotion,
pause/resume, rollback, reauth start/success/failure en retry. Auditdetails
vullen veilige `result`- en `reason_code`-velden aan en redigeren secrets.
Connector sync telemetry bevat provider, connectorversie, resource, status,
duur, foutcategorie, retries en rate-limitcount; connection IDs worden alleen
als verkorte SHA-256-hash geëxporteerd. Documentatie, CI-gates en het
credential-vrije `connector-lifecycle-evidence.json` artifact zijn toegevoegd.

### User story

Als beheerder wil ik kunnen reconstrueren waarom een connector ongezond was,
welke actie is uitgevoerd en of de lifecycle-release veilig is, zonder
gevoelige data te verzamelen.

### Implementatie

- Audit alle lifecycle-mutaties: catalogus promotion, enable/disable,
  rollback, reauth start/success/failure, retry en pause/resume. Gebruik de
  bestaande tenant-scoped audit infrastructuur en actor/permissievelden.
- Voeg metrics/logvelden toe voor provider, connectorversie, connection ID,
  resource, status, duration, retries, rate-limit count en sync outcome.
  Hash of pseudonimiseer waar een connection ID in externe telemetry terecht
  komt.
- Werk `docs/CONNECTOR_VERSION_LIFECYCLE.md`,
  `docs/connector-api.md`, `docs/connections.md`, `docs/DATABASE.md` en
  `docs/API.md` bij met het definitieve contract, statusprecedence,
  reauth-flow, retention/redaction en rollback-runbook.
- Voeg CI-gates toe voor lifecycle-configvalidatie, connector contracttests,
  migration upgrade, OpenAPI diff, secret-redaction en een synthetische
  end-to-end flow.
- Voeg een release-evidence-artifact toe met connectorversie, certificatie-
  commit, fixtureversie, testresultaat, canary-resultaat en rollbackversie.

### Acceptatiecriteria

- [x] Elke lifecycleactie heeft tenant, actor, provider, connection,
  timestamp, resultaat en veilige reason code in de auditlog.
- [x] Audit en telemetry bevatten geen credentialwaarden, tokens, financiële
  payloads of volledige providerfoutteksten.
- [x] CI faalt bij ongeldig lifecycle-config, ontbrekende connectorcertificatie,
  migration regressie, OpenAPI breaking change of secret leakage.
- [x] Een synthetische test voert minstens uit: catalogus → connection test →
  resource sync → rate-limit diagnosis → reauth → healthy verwerking.
- [x] Documentatie beschrijft expliciet dat provider health drie niveaus heeft
  en dat `connected` niet gelijkstaat aan succesvolle verwerking.

Verificatie: 62 gerichte lifecycle/audit/health/release/observability-tests
geslaagd; gewijzigde bestanden zijn Ruff-clean, Pyright meldt 0 errors en
`git diff --check` is groen. Het evidence-script is uitgevoerd en produceert
alle gecertificeerde connectorversies met certificatie-commit, fixtureversie,
testresultaat, canary-resultaat en rollbackversie. De repositorybrede Ruff-run
blijft bestaande, niet-gerelateerde legacy-fouten in `deploy/` en oudere
scripts rapporteren; die blokkeren de story niet.

## Datamodel- en compatibiliteitsrichtlijnen

- Gebruik een expand/contract Alembic-migratie. Voeg nullable velden en
  tabellen eerst toe; backfill veilig vanuit `Credential`, `SyncRun` en
  `config/connector-lifecycle.json`; maak constraints pas verplicht nadat de
  worker/API dual-read hebben gedraaid.
- Houd alle nieuwe state tenant-scoped. Een providerstatus mag nooit tussen
  twee connections met dezelfde provider worden gedeeld, tenzij de rate-limit
  policy expliciet `provider`-scope aangeeft.
- Gebruik UTC `timestamptz` voor expiry, retry en freshness. Gebruik vaste
  reason codes in API-contracten en vertaalbare teksten alleen in de UI.
- Maak healthprojecties read-only voor gebruikers. Alleen een sync,
  reauthenticatie of operatoractie mag de onderliggende status veranderen.
- Behoud legacy clients: geen verwijdering of semantische wijziging van
  bestaande responsevelden; voeg nieuwe velden optioneel toe.

## Aanbevolen verificatie per wave

```text
ruff check .
pyright
pytest -q tests/connectors tests/test_plugin_integration.py
pytest -q tests/test_connectors_multi_connection.py tests/test_connection_audit.py
pytest -q tests/integration
python scripts/generate_openapi.py
python scripts/connector_lifecycle.py --config config/connector-lifecycle.json
```

De implementerende agent moet per story de werkelijk uitgevoerde commando's,
de nieuwe migratie-head, relevante testcount en eventuele bewust uitgestelde
provider-specifieke expiry-hooks in de PR beschrijven. Geen story is done
wanneer alleen de UI-status is aangepast zonder server-side statusregels en
tests.
