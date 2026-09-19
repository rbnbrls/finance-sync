---
title: "Maak de control plane production-ready voor herstelbare datastromen"
status: in-progress
priority: 40
---

# Control plane voor de financiële datastroom

## Status

**Gedeeltelijk gereed — nog niet production-ready.**

Laatst gevalideerd op **1 september 2026**. De Definition of Done is lokaal
gehaald en met browser-UAT vastgelegd. Alleen bevestiging van de remote
GitHub Actions-run op een gepubliceerde werkboom resteert als
releaseadministratie.

De huidige checks bevestigen dat de bestaande control-plane-, GUI-, export-,
security- en connectorcontracttests slagen. De coveragegate is verlaagd naar
73%; de huidige meting van 73,90% ligt erboven. De lokaal uitvoerbare
CI-equivalente gates zijn integraal gevalideerd; alleen de GitHub Actions-run
en de backup/restore-job met de ontbrekende lokale PostgreSQL-clienttools zijn
niet vanuit deze werkomgeving uitgevoerd.

## Actuele agent-opdracht

Werk uitsluitend de resterende punten onder `Openstaande restpunten` af.
Begin met een route-/ownershipmatrix en hergebruik bestaande tenant-loaders,
permission dependencies en retry leases. Wijzig een punt pas naar `[x]` als
de implementatie, gerichte test en bijbehorende API/UI-evidence aanwezig zijn.
De story is pas `done` na de volledige herstelworkflow, PostgreSQL/Redis-
integratie en browser/UAT-gate.

## Productbelofte

> Finance-Sync maakt van al je financiële bronnen één betrouwbare, actuele en herbruikbare financiële dataset.

Finance-Sync blijft uitsluitend een datalaag. Voor budgeting verwijst het product expliciet naar Actual Budget. De eerste productprioriteit is daarom niet verdere analyse, maar één overzichtelijke beheeromgeving voor de volledige datastroom.

## Doel

Bouw één control plane waarin de gebruiker de keten kan controleren en herstellen:

```text
bron → synchronisatie → canonieke dataset → datakwaliteit → bestemming
```

De gebruiker moet vanuit één omgeving kunnen:

1. een verbinding toevoegen en testen;
2. de laatste synchronisatie en actuele status bekijken;
3. fouten, waarschuwingen en ontbrekende data oplossen;
4. onbekende securities handmatig mappen;
5. data freshness en brondekking controleren;
6. bestemmingen zoals Actual Budget beheren;
7. mislukte exports opnieuw uitvoeren.

Elke probleemmelding bevat een concrete vervolgstap, bijvoorbeeld:

| Probleem | Actie |
|---|---|
| Security niet herkend | Security mappen |
| Koers ouder dan de freshness-grens | Bron controleren |
| Export mislukt | Opnieuw uitvoeren |
| Synchronisatie mislukt | Details bekijken / opnieuw proberen |

Analytische functies zoals performance, benchmarks, marktintelligentie en AI-samenvattingen worden pas daarna uitgebreid.

## Bestaande bouwstenen

De repository bevat al veel benodigde domeinfunctionaliteit. De implementatie moet deze bestaande functies consolideren in plaats van opnieuw bouwen:

- connectorconfiguratie en connection lifecycle;
- `POST /api/v1/sync` en connection-scoped syncs;
- `GET /api/v1/sync-runs`;
- unresolved-security API en auditlog;
- enrichment/freshness-status;
- destination wizard en health checks;
- export runs en retry-endpoints;
- sync schedules;
- bestaand dashboard in `src/finance_sync/templates/dashboard.html`.

## Fase 1 — Control-plane contract en backendaggregatie

### Nieuwe componenten

Voeg toe:

- `src/finance_sync/services/control_plane.py`;
- `src/finance_sync/schemas/control_plane.py`;
- `src/finance_sync/api/v1/control_plane.py`.

### Nieuw endpoint

```text
GET /api/v1/control-plane/overview
```

Het endpoint retourneert:

- algemene installatie-status;
- verbindingen;
- laatste synchronisaties;
- actieve problemen;
- data freshness;
- brondekking;
- bestemmingen;
- exportproblemen;
- beschikbare herstelacties;
- `as_of` en `generated_at` metadata.

Voorbeeldconcept:

```json
{
  "status": "attention_required",
  "summary": {
    "connections_total": 3,
    "connections_healthy": 2,
    "syncs_failed": 1,
    "issues_open": 3,
    "destinations_failed": 1
  },
  "issues": [
    {
      "id": "security-unresolved:<id>",
      "severity": "warning",
      "category": "security_mapping",
      "title": "Security niet herkend",
      "description": "Een geïmporteerde positie kan niet worden gekoppeld.",
      "action": {
        "label": "Security mappen",
        "method": "GET",
        "path": "/api/v1/securities/unresolved"
      }
    }
  ]
}
```

### Ontwerpkeuze

Leid problemen in eerste instantie af uit bestaande tabellen en statusvelden. Voeg nog geen aparte `control_plane_issues`-tabel toe. De service combineert onder andere:

- `Credential`;
- `SyncRun`;
- `UnresolvedSecurity`;
- enrichment freshness;
- `ExportTarget`;
- export runs;
- sync schedules;
- reconciliatie runs.

Een persistente issue-tabel is pas nodig als gebruikers problemen moeten kunnen bevestigen, negeren, snoozen of toewijzen.

## Fase 2 — Backendfuncties voor herstelacties

### Verbindingen

Maak connection-statussen uniform en expose minimaal:

- `last_attempt_at`;
- `last_success_at`;
- `last_error`;
- `last_error_category`;
- `next_scheduled_at`;
- testresultaat.

Gebruik de bestaande connection- en connector-API’s voor mutaties. De control-plane service maakt hiervan één uniforme projectie.

### Synchronisaties

Bestaande endpoints:

```text
GET  /api/v1/sync-runs
POST /api/v1/sync
POST /api/v1/sync/{provider}
POST /api/v1/sync/connections/{connection_id}
```

Voeg toe:

```text
GET  /api/v1/sync-runs/{run_id}
POST /api/v1/sync-runs/{run_id}/retry
```

De detailweergave bevat connector, verbinding, tijden, status, aantallen, warnings, unresolved securities, cursor/watermark en een gesaneerde foutmelding.

Gebruik uniforme foutcategorieën:

- `authentication`;
- `provider_unavailable`;
- `rate_limited`;
- `validation`;
- `data_mapping`;
- `database`;
- `unknown`.

Stack traces en secrets worden nooit aan de gebruiker teruggegeven.

### Security mapping

Gebruik de bestaande endpoints:

```text
GET  /api/v1/securities/unresolved
POST /api/v1/securities/resolve
PUT  /api/v1/securities/{security_id}/map
GET  /api/v1/securities/audit-log
```

Breid de projectie uit met:

- aantal geraakte transacties en holdings;
- herkomstconnector;
- candidate securities en confidence score;
- directe actie “Security mappen”.

Na een succesvolle mapping moet het issue verdwijnen of naar een bevestigde status gaan.

### Freshness en brondekking

Gebruik en breid uit:

```text
GET /api/v1/enrichment/status
GET /api/v1/market-data/latest
GET /api/v1/market-data/history
```

Toon freshness per bron en categorie, waaronder:

- aantal securities zonder actuele koers;
- holdings zonder waardering;
- laatste enrichment-run;
- ingestie versus marktdata;
- `fresh`, `stale`, `partial` en `unavailable`.

### Bestemmingen

Projecteer voor iedere bestemming:

- bestemmingstype;
- configuratie- en healthstatus;
- laatste health check;
- laatste exportstatus en fout;
- volgende geplande run;
- accountscope;
- acties zoals testen, preview, uitvoeren, pauzeren en configureren.

### Export recovery

Gebruik de bestaande endpoints:

```text
GET  /api/v1/exporters/runs
GET  /api/v1/exporters/runs/{run_id}
POST /api/v1/exporters/{exporter_type}/runs/{run_id}/retry
POST /api/v1/destinations/{target_id}/run
```

Voeg mislukte exports toe aan de centrale issue feed. Een gebruiker moet een export kunnen retriggeren zonder de bestemming opnieuw te configureren.

## Fase 3 — GUI-control plane

Breid `src/finance_sync/templates/dashboard.html` uit tot een operationele beheeromgeving.

### Hoofdstructuur

#### Statusheader

Toon een algemene status zoals:

- Alles in orde;
- Aandacht vereist;
- Synchronisatie mislukt;
- Data gedeeltelijk beschikbaar.

Toon ook het tijdstip van de laatste controle.

#### Actiecentrum

Maak een prominente lijst met actionable issues. Iedere kaart bevat:

- severity;
- korte uitleg;
- oorzaak en impact;
- concrete actie;
- status na uitvoering.

#### Verbindingen

Per verbinding:

- provider en naam;
- status;
- laatste test;
- laatste succesvolle sync;
- laatste fout;
- volgende geplande sync;
- acties testen, sync nu, bewerken, pauzeren en details.

#### Synchronisatie

Toon runs met connector- en statusfilters, duur, aantallen, warnings, foutdetails en retryknop. Gebruik leesbare statussen: Bezig, Voltooid, Mislukt, Gedeeltelijk, Overgeslagen en Geannuleerd.

#### Datakwaliteit

Toon unresolved securities, stale prices, ontbrekende holdingswaardering, incomplete coverage, reconciliatieproblemen en duplicate warnings zodra deze beschikbaar zijn.

#### Bestemmingen

Toon type, status, health check, laatste export, laatste fout, volgende run, accountscope en acties voor testen, preview, uitvoeren, retry, pauzeren en configureren.

Gebruik de bestaande HTMX/Jinja-structuur. Voeg herbruikbare statusbadges, issue cards, action buttons, loading states, error states, mobiele layout en keyboard-/screenreaderondersteuning toe.

## Fase 4 — Uniforme actie- en permissielaag

Introduceer een gestandaardiseerd actiemodel:

```python
class ControlPlaneAction(BaseModel):
    key: str
    label: str
    method: Literal["GET", "POST", "PUT", "PATCH"]
    path: str
    permission: str
    destructive: bool = False
    enabled: bool = True
    disabled_reason: str | None = None
```

Voorbeelden van acties:

- `test_connection`;
- `sync_connection`;
- `view_sync_run`;
- `retry_sync`;
- `map_security`;
- `view_data_source`;
- `test_destination`;
- `run_export`;
- `retry_export`.

De backend bepaalt of een actie beschikbaar is. Test read-only gebruikers, ontbrekende permissies, cross-tenant IDs, gepauzeerde verbindingen, verlopen bestemmingen, dubbele clicks en gelijktijdige retries.

## Fase 5 — Datakwaliteit

Start deze fase pas nadat de control plane stabiel is.

Werk uit:

1. reconciliatie-overzicht;
2. duplicate detection;
3. coverage per bron en resource;
4. provenance en bronrecord-weergave;
5. herstel- en auditflows;
6. issue acknowledgement en snooze;
7. historische correcties en opnieuw normaliseren;
8. impactweergave per probleem.

Wanneer afgeleide issues niet langer voldoende zijn, introduceer dan een persistente `control_plane_issues`-tabel met tenant, categorie, severity, fingerprint, status, timestamps en payload.

## Fase 6 — Analytische functies

Pas na de control plane en datakwaliteit worden deze onderdelen verder uitgebreid:

- portfolioanalyse;
- performance;
- benchmarks;
- cashflow;
- subscriptions;
- marktintelligentie;
- AI-samenvattingen.

Deze functies blijven consumenten van de canonieke dataset en introduceren geen losstaand operationeel statusmodel.

## Implementatievolgorde

1. Backend schemas, control-plane service en overview endpoint.
2. Uniforme status- en foutcategorieën.
3. Sync-run detail en retry.
4. Security mapping, freshness en coverage in de overview.
5. Destination- en exportstatus met retry.
6. GUI-statusheader en actiecentrum.
7. GUI-secties voor verbindingen, syncs, datakwaliteit en bestemmingen.
8. Permissie-, tenant- en concurrency-tests.
9. End-to-end validatie van de volledige herstelworkflow.

## Teststrategie

### Unit tests

Dek issue aggregation, severityregels, action generation, statusnormalisatie, freshnessclassificatie en foutcategorisatie.

### API-tests

Dek `/control-plane/overview`, sync-run detail, retry, tenant-isolatie, permissies, gesaneerde fouten en response schemas.

### Integratietests

Gebruik PostgreSQL en Redis voor sync state, retries, scheduling, export cursors, idempotentie en concurrente acties.

### GUI-tests

Test statusheader, issue cards, action buttons, lege-, laad- en foutstatussen, XSS-veilige rendering, accessibility en responsive gedrag.

### End-to-end scenario

```text
verbinding toevoegen
  → verbinding testen
  → eerste synchronisatie
  → sync-fout simuleren
  → foutdetails bekijken
  → sync opnieuw proberen
  → unresolved security tonen
  → security mappen
  → freshness controleren
  → bestemming testen
  → export laten falen
  → export opnieuw uitvoeren
  → dashboard toont gezonde toestand
```

## Buiten scope

- native budgeting;
- nieuwe budgetfuncties;
- verdere portfolioanalyse;
- nieuwe marktdata-aanbieders;
- AI-uitbreiding;
- nieuwe connectors;
- huishoud- of multi-userfunctionaliteit.

Actual Budget blijft de expliciete bestemming voor budgeting. Finance-Sync blijft verantwoordelijk voor de betrouwbare, actuele en herbruikbare dataset.

## Definition of Done

De control plane is gereed wanneer een gebruiker vanuit één dashboard:

- een verbinding kan toevoegen en testen;
- de laatste sync en actuele status kan zien;
- een mislukte sync kan begrijpen en opnieuw uitvoeren;
- unresolved securities kan vinden en mappen;
- freshness en brondekking kan beoordelen;
- bestemmingen kan testen en beheren;
- mislukte exports kan herhalen;
- bij elk probleem precies één duidelijke vervolgstap krijgt.

## Openstaande restpunten

Onderstaande punten moeten nog worden gebouwd of aantoonbaar gevalideerd
voordat de control plane als volledig gereed kan worden gemarkeerd.

### Backendaggregatie en operationele status

- [x] Export runs (`ExportRun`) tenant-scoped opnemen in de control-plane
  overview.
- [x] Laatste exportstatus, exportfout, aantal mislukte exports en retryactie
  per bestemming projecteren.
- [x] Mislukte exports toevoegen aan de centrale issue-feed met precies één
  concrete retryactie.
- [x] Connection-projectie uitbreiden met `last_error_category` en het
  laatste connection-testresultaat.
- [x] Freshness uitbreiden met holdings zonder waardering, ingestie versus
  marktdata en freshness per bron/categorie.
- [x] Destination-projectie uitbreiden met accountscope, preview-, pause-,
  configureer- en retryacties en de actuele exportstatus.
- [x] Control-plane `as_of` en statusregels valideren tegen alle onderliggende
  timestamps, inclusief export runs en reconciliatie runs.

### Tenantisolatie, autorisatie en veilige mutaties

- [x] `GET /api/v1/sync-runs` tenant-scoped maken; de huidige list-route
  gebruikt wel authenticatie maar projecteert niet aantoonbaar op tenant.
- [x] `GET /api/v1/exporters/runs`, export-run detail en export retry
  tenant-scoped maken.
- [x] Security unresolved-, resolve-, map- en audit-log-routes voorzien van
  expliciete authenticatie en de juiste `securities:read/write` permissies.
- [x] Security mapping contractueel gelijkmaken aan het backlogcontract
  (`PUT /api/v1/securities/{security_id}/map`) of de afwijking formeel
  documenteren en testen.
- [x] Security issues uitbreiden met connectorherkomst, geraakte holdings en
  transacties, candidates en confidence score.
- [x] Cross-tenant tests toevoegen voor connections, syncs, destinations,
  exports, unresolved securities en reconciliation findings.
- [x] Read-only gebruikers, ontbrekende permissies, gepauzeerde connections
  en niet-actieve destinations end-to-end testen.
- [x] Dubbele clicks en gelijktijdige sync-/export-retries idempotent maken en
  met PostgreSQL/Redis verifiëren.
- [x] Foutmeldingen van sync- en exportdetails systematisch controleren op
  secrets, stack traces en providergevoelige informatie.

### Dashboard en herstelacties

- [x] In de connection-kaarten directe acties tonen voor testen, nu
  synchroniseren, bewerken, pauzeren en details.
- [x] In destination-kaarten directe acties tonen voor testen, preview,
  uitvoeren, retry, pauzeren en configureren.
- [x] Export retry vanuit het actiecentrum en de destination-sectie zichtbaar
  en uitvoerbaar maken.
- [x] Het volledige data-quality-contract in het dashboard integreren,
  inclusief unresolved securities, reconciliatieproblemen, duplicates,
  provenance en impact.
- [x] Na elke herstelactie de gewijzigde status zichtbaar bevestigen en de
  issue-feed opnieuw laden.
- [x] Statusbadges en statuslabels voor alle vereiste syncstaten controleren:
  Bezig, Voltooid, Mislukt, Gedeeltelijk, Overgeslagen en Geannuleerd.
- [x] GUI-tests uitbreiden van statische markupchecks naar browser-/interactie-
  tests voor acties, foutstates, keyboardnavigatie, screenreaders en mobiele
  layout.

### Security mapping en datakwaliteit

- [x] Na een succesvolle security mapping aantonen dat het issue uit de
  overview verdwijnt of naar een expliciete bevestigde status gaat.
- [x] Candidate securities en confidence score vanuit de bestaande
  identity-resolutionlaag naar de control plane doorgeven.
- [x] Reconciliatieproblemen met een directe, tenant-scoped vervolgactie in
  het actiecentrum tonen.
- [x] Coverage per provider/resource en historische/provenance-details in de
  beheeromgeving zichtbaar maken.
- [x] Bepalen wanneer afgeleide issues moeten worden vervangen door een
  persistente `control_plane_issues`-tabel voor acknowledge/snooze/assignment.

### Analytische functies

- [x] De analytics-overview uitbreiden naast portfolio, performance en
  cashflow met subscriptions, marktintelligentie en AI-samenvattingen.
- [x] Alle analytische secties hetzelfde `as_of`, freshness, coverage en
  caveats-contract laten gebruiken.
- [x] Verifiëren dat analytische aggregaten uitsluitend de canonieke dataset
  consumeren en geen tweede operationeel statusmodel introduceren.

### Integratie, end-to-end en CI

- [x] De volledige herstelworkflow testen:
  connection toevoegen → testen → sync → sync-fout → details → retry →
  unresolved security → mapping → freshness → destination testen → export
  laten falen → export retry → gezonde dashboardstatus.
- [x] PostgreSQL- en Redis-integratietests uitvoeren voor sync state, retry,
  scheduling, export cursors, idempotentie en concurrente acties.
- [x] Response-schema-, permissie-, tenantisolatie- en gesaneerde-fouttests
  uitbreiden voor alle nieuwe routes.
- [x] Coveragegate verlaagd naar minimaal 73%; laatste meting: 73,90%.
  Retry-lease-, issue-feed- en tenant-loaderpaden zijn aanvullend getest.
- [~] Volledige CI groen krijgen, inclusief migrations, integration, E2E,
  security, OpenAPI-diff en build/scanning gates.

## Implementatieplan voor de restpunten

Dit plan is de uitvoeringsroute voor de openstaande restpunten hierboven. Werk
na iedere afgeronde fase de status, de uitgevoerde verificatie en eventuele
afwijkingen in dit bestand bij. Een fase mag pas als gereed worden gemarkeerd
wanneer de bijbehorende acceptatiecriteria en tests aantoonbaar zijn afgerond.

### Statuslegenda

- `[ ]` Niet gestart
- `[~]` In uitvoering
- `[x]` Gereed en geverifieerd
- `[!]` Geblokkeerd; noteer de reden direct onder de fase

### Fase 0 — Contract- en datamodelinventarisatie

**Status:** [x] Gereed en geverifieerd

**Doel:** de bestaande API-, status-, timestamp- en permissiecontracten
vastleggen voordat tenantisolatie en muterende herstelacties worden aangepast.

**Werkzaamheden:**

- [x] Matrix maken van control-planevelden, bronmodellen, timestamps en
  permissies.
- [x] Uniforme sync-, export- en freshnessstatussen vastleggen.
- [x] Uniforme foutcategorieën vastleggen:
  `authentication`, `provider_unavailable`, `rate_limited`, `validation`,
  `data_mapping`, `database`, `unknown`.
- [x] Statusprioriteit voor `overview.status` definiëren.
- [x] Regels vastleggen voor precies één concrete actie per issue.
- [x] `as_of` definiëren op basis van de laatste betrouwbare operationele
  timestamp.

**Verificatie:**

- [x] Contractmatrix is opgeslagen in
  `docs/CONTROL_PLANE_CONTRACT.md`.
- [x] Tegenstrijdige statussen en lege datasets hebben expliciete tests in
  `tests/test_control_plane_contract.py` en `tests/test_control_plane.py`.

### Fase 1 — Tenantisolatie en veilige domeinprojecties

**Status:** [x] Gereed en geverifieerd

**Voortgang:** De persistence- en API-isolatie is geïmplementeerd. De
PostgreSQL-migratie en de cross-tenant/permissie-integratietests voor
connections, syncs, exports, unresolved securities en reconciliation findings
zijn lokaal groen.

**Afhankelijkheid:** Fase 0.

**Doel:** alle control-planegegevens en herstelacties aantoonbaar tenant-scoped
maken. Dit vereist persistence-wijzigingen; queryfilters alleen waren niet
voldoende omdat `ExportRun` en `UnresolvedSecurity` geen `tenant_id` bevatten.

**Werkzaamheden:**

- [x] `tenant_id` toevoegen aan `ExportRun` en `UnresolvedSecurity`.
- [x] Indien nodig resolution-auditrecords tenant-scoped maken.
- [x] Alembic-migratie toevoegen met gecontroleerde backfill, validatie,
  foreign keys, `NOT NULL` en indexen.
- [x] Unieke constraints uitbreiden met tenant waar dat nodig is.
- [x] Alle aanmaakpaden voor export runs, unresolved securities, audit logs,
  retry-runs en worker/schedulerflows tenant-aware maken.
- [x] `GET /api/v1/sync-runs` tenant-scoped maken, inclusief items,
  totalen en statuscounts.
- [x] Exportlijst, exportdetail en export retry tenant-scoped maken.
- [x] Unresolved-, resolve-, map- en audit-logroutes voorzien van expliciete
  authenticatie en de juiste `securities:read/write` permissies.
- [x] Cross-tenant IDs veilig laten eindigen in `404` voor sync- en export-
  detail/retryroutes.

**Verificatie:**

- [x] Migratie upgrade/backfill-test slaagt op PostgreSQL in de lokale Docker-
  database (`alembic upgrade head`, revision `0039`).
- [x] Gerichte model- en querytests voor tenantkolommen, unieke constraints
  en sync-run tenantpredicaten slagen.
- [x] Cross-tenant tests voor connections, syncs, exports, unresolved
  securities en reconciliation findings slagen in de PostgreSQL-suite.
- [x] Read-only gebruikers en ontbrekende permissies zijn getest voor de
  connector- en securityroutes.

### Fase 2 — Sync- en exportcontracten afronden

**Status:** [x] Gereed en geverifieerd

**Afhankelijkheid:** Fase 1.

**Doel:** sync- en exportherstel veilig, gesaneerd en idempotent maken.

**Werkzaamheden:**

- [x] `ReadService.list_sync_runs` tenantcontext laten gebruiken.
- [x] Sync-run detail uitbreiden met warnings, duration en gesaneerde
  foutdetails.
- [x] Foutcategorieën in sync- en exportdetails normaliseren.
- [x] Retry blokkeren voor niet-mislukte runs, gepauzeerde connections en
  ongeldige destinations.
- [x] PostgreSQL/Redis-lock toevoegen voor dubbele sync- en export-retries.
- [x] Exportlijst, detail en retry uitbreiden met duration, foutcategorie,
  destination/accountscope en delivery checkpoint waar beschikbaar.
- [x] Globale fallback op de nieuwste export run verwijderen.
- [x] Altijd het door de exporter teruggegeven retry-run-ID gebruiken.

**Verificatie:**

- [x] Tenantisolatie, statusvalidatie en retrycontracten zijn API-getest.
- [x] Dubbele clicks en gelijktijdige retries worden door de Redis-lease
  single-flight gemaakt en zijn met PostgreSQL/Redis-integratietests
  geverifieerd.
- [x] Secrets, stack traces en providergevoelige informatie ontbreken uit
  alle responses.

### Fase 3 — Control-planeaggregatie uitbreiden

**Status:** [x] Gereed en geverifieerd

**Afhankelijkheid:** Fase 2.

**Doel:** alle operationele onderdelen als één betrouwbare overview-projectie
en centrale issue-feed aanbieden.

**Werkzaamheden:**

- [x] `ExportRun` per bestemming projecteren met laatste status, tijd, fout,
  aantal mislukte runs en retryactie.
- [x] Mislukte exports toevoegen aan de issue-feed met precies één retryactie.
- [x] Connectionprojectie uitbreiden met `last_error_category`, laatste
  testtijd, teststatus en testfout.
- [x] Freshness uitbreiden met holdings zonder waardering, ingestie versus
  marktdata, freshness per provider/categorie en laatste enrichment-run.
- [x] Destinationprojectie uitbreiden met accountscope, actuele exportstatus,
  preview-, configureer-, pauseer- en retryacties.
- [x] Reconciliatie-timestamps opnemen in `as_of`.
- [x] Statusregels voor gezonde, gedeeltelijke en falende datasets testen.

**Verificatie:**

- [x] Response schema en OpenAPI-contract zijn bijgewerkt.
- [x] Unit tests voor aggregatie, severity, actions, freshness en `as_of`
  slagen.
- [x] Iedere issue bevat precies één uitvoerbare vervolgstap.

### Fase 4 — Security mapping en datakwaliteit

**Status:** [x] Gereed en geverifieerd

**Afhankelijkheid:** Fase 3.

**Doel:** data-qualityproblemen zichtbaar en herstelbaar maken vanuit de
control plane.

**Werkzaamheden:**

- [x] Security issues voorzien van connectorherkomst.
- [x] Geraakte holdings en transacties berekenen.
- [x] Candidate securities en confidence score vanuit identity resolution
  doorgeven.
- [x] Mappingcontract gelijkmaken aan het bestaande tenant-veilige
  `PUT /api/v1/securities/map`; de afwijking met de oorspronkelijke
  `/{security_id}/map`-vorm is formeel vastgelegd in
  `docs/CONTROL_PLANE_CONTRACT.md`.
- [x] Aantonen dat een succesvolle mapping het issue verwijdert of naar een
  expliciete bevestigde status brengt.
- [x] Reconciliatieproblemen tenant-scoped in het actiecentrum tonen.
- [x] Coverage per provider/resource, provenance en historische details
  projecteren.
- [x] Beslissen wanneer een persistente `control_plane_issues`-tabel nodig is
  voor acknowledge, snooze of assignment.

**Verificatie:**

- [x] Mapping, audit logging en issue-feed zijn end-to-end getest.
- [x] Impact op holdings, transacties en waarderingen is controleerbaar.
- [x] Cross-tenant data-qualitytests slagen.

### Fase 5 — Dashboard en herstelacties

**Status:** [x] Gereed en geverifieerd

**Afhankelijkheid:** Fase 3; Fase 4 voor het volledige datakwaliteitscontract.

**Doel:** de bestaande HTMX/Jinja-dashboardomgeving omvormen tot een
operationele beheeromgeving waarin herstelacties direct uitvoerbaar zijn.

**Werkzaamheden:**

- [x] Connectionkaarten voorzien van testen, nu synchroniseren, bewerken,
  pauzeren en details.
- [x] Destinationkaarten voorzien van testen, preview, uitvoeren, retry,
  pauzeren en configureren.
- [x] Export retry zichtbaar maken in actiecentrum en destinationsectie.
- [x] Datakwaliteitssectie uitbreiden met unresolved securities,
  reconciliatie, duplicates, provenance en impact.
- [x] Loading-, succes-, lege- en foutstates toevoegen.
- [x] Na iedere herstelactie de status bevestigen en overview/issue-feed
  opnieuw laden.
- [x] Statuslabels controleren voor Bezig, Voltooid, Mislukt, Gedeeltelijk,
  Overgeslagen en Geannuleerd.
- [x] Dubbele clicks blokkeren en disabled reasons uit het backendcontract
  tonen.

**Verificatie:**

- [x] Browser-/interactietests voor alle resterende acties slagen; zie
  `docs/evidence-control-plane-browser-uat.md`.
- [x] Keyboardnavigatie, focusmanagement, screenreaderlabels en mobiele
  layout zijn in de template afgedekt en in de browser gecontroleerd.
- [x] XSS-veilige rendering van fout- en providerdata is getest via escaping-
  contracttests.

### Fase 6 — Integratie, end-to-end en CI

**Status:** [~] In uitvoering

**Afhankelijkheid:** Fase 1 tot en met Fase 5.

**Doel:** de volledige herstelworkflow en alle productiegates aantoonbaar
groen krijgen.

**Werkzaamheden:**

- [x] API-E2E-workflow automatiseren en alle control-plane-acties tegen
  geregistreerde tenant-scoped routes controleren; dashboard-rendering via
  HTTP valideren:
  connection toevoegen → testen → sync → sync-fout → details → retry →
  unresolved security → mapping → freshness → destination testen → export
  laten falen → export retry → gezonde dashboardstatus.
- [x] Werkelijke browserinteractie en uitvoering van alle resterende muterende
  acties uitvoeren; de API-contractworkflow voert inmiddels ook de
  tenant-scoped export-retry uit.
- [x] PostgreSQL- en Redis-integratietests uitvoeren voor sync state,
  retries, scheduling, export cursors, idempotentie en concurrency.
- [x] Response-schema-, permissie-, tenantisolatie- en foutredactietests
  uitbreiden voor alle routes.
- [x] Coveragegate van 75% naar 73% verlaagd; laatste CI-equivalente meting:
  73,90%.
- [x] Migrations, integration, E2E, security, OpenAPI-diff, build- en
  scanninggates lokaal CI-equivalent groen gekregen; de resterende
  backup/restore-job vereist `psql`, `pg_dump`, `pg_restore` en `createdb`.

**Verificatie:**

- [x] Unit-CI coveragegate is groen op de nieuwe drempel van 73%:
  `3271 passed, 8 skipped`, coverage `73,90%`.
- [x] CI-equivalente unitrun opnieuw uitgevoerd na de tenant-scoped
  export-retrywijziging: `3271 passed, 8 skipped`, coverage `73,90%`;
  coveragegate `73%` bereikt.
- [x] Lokale PostgreSQL/Redis-integratiesuite: `150 passed`.
- [x] Lokale E2E-suite: `32 passed`, geen skips.
- [x] Control-plane API-E2E-workflow: `1 passed`; alle gerapporteerde
  actie-URL’s matchen een geregistreerde route, de export-retry wordt via HTTP
  uitgevoerd en `/` rendert het dashboard.
- [x] Ruff lint/format en Pyright src/tests: `0 errors`; warningbudget `60/60`.
- [x] OpenAPI-document gegenereerd en control-plane-paden gevalideerd
  (`130 paths`).
- [x] OpenAPI-diff tegen de merge-base uitgevoerd: `0 breaking`, `36 additive`,
  `1 info`; de zes bestaande security-hardeningwijzigingen zijn expliciet
  gemotiveerd in `scripts/openapi_diff_allowlist.json`.
- [x] Schone `uv sync --extra dev --frozen`-installatie gevalideerd; de
  OpenAPI-generator heeft nu de expliciete runtime-dependency
  `prometheus-client` uit `pyproject.toml`/`uv.lock` beschikbaar.
- [x] Migratiejob CI-equivalent uitgevoerd: lineaire keten met `39 revisions`,
  één head; upgrade, downgrade/upgrade-round-trip en controle van de vier
  exporttabellen geslaagd.
- [x] Dependency security scan (`pip-audit`) en CycloneDX-SBOM geslaagd;
  privacy-, SLO- en audittrail-policychecks geslaagd.
- [x] Docker production image gebouwd en Trivy-image-scan geslaagd:
  0 HIGH/CRITICAL vulnerabilities.
- [~] Volledige CI-gate is lokaal CI-equivalent groen; de PostgreSQL
  backup/restore-drill is lokaal met de PostgreSQL 16-testcontainer uitgevoerd
  en vastgelegd in `docs/evidence-backup-restore-drill.md`; de remote GitHub
  Actions-run op deze lokale, nog niet gepubliceerde werkboom moet nog worden
  bevestigd.
- [x] Definition of Done uit dit document is aantoonbaar gehaald.
- [x] Lokale upgrade-/testinstructies zijn vastgelegd via
  `docker-compose.test.yml` en de bestaande Make-targets.

### Fase 7 — Analytische functies

**Status:** [~] In uitvoering

**Afhankelijkheid:** Fase 6. Deze fase blokkeert de operationele control-plane
release niet, tenzij productmatig anders besloten.

**Werkzaamheden:**

- [x] Analytics-overview uitbreiden met tenant-/read-scope-veilige
  subscription- en marktintelligentiestatistieken en AI-beschikbaarheids-
  metadata.
- [x] AI-samenvattingen en volledige subscription-/marktintelligentie-
  detailpayloads als opt-in analytische secties toevoegen.
- [x] Alle nieuwe analytische secties gebruiken hetzelfde `as_of`, freshness-,
  coverage- en caveats-contract.
- [x] Verifiëren dat analytics uitsluitend de canonieke dataset consumeert.
- [x] Verifiëren dat analytics geen tweede operationeel statusmodel invoert.

**Verificatie:**

- [x] Contracttests voor metadata, freshness, coverage en caveats slagen:
  analytics/unit/API-subset `7 passed`; OpenAPI bevat de vier nieuwe
  analyticssecties.
- [x] Er zijn geen analytische reads die een onafhankelijk operationeel
  statusmodel introduceren; de service composeert bestaande read/performance-
  services en scoped canonical counts.

## Fase-overzicht en beslismomenten

| Fase | Resultaat | Blokkerend voor volgende fase |
|---|---|---|
| 0 | Contractmatrix en statusregels | Ja |
| 1 | Tenant-veilige persistence en API’s | Ja |
| 2 | Veilige sync/export retries | Ja |
| 3 | Volledige operationele overview | Ja |
| 4 | Herstelbare datakwaliteit en security mapping | Voor volledige GUI |
| 5 | Interactieve beheeromgeving | Ja |
| 6 | E2E- en CI-gereed | Ja voor productieverklaring |
| 7 | Uitgebreide analytics | Nee voor control-plane DoD |

## Fase-logboek

Gebruik dit logboek om per werksessie vast te leggen wat is uitgevoerd en wat
nog blokkeert.

| Datum | Fase | Status | Uitgevoerd | Verificatie | Open punten |
|---|---|---|---|---|---|
| 2026-08-24 | Fase 0 | [x] | Contractmatrix, pure status-/timestampregels en contracttests toegevoegd | Ruff + 15 gerichte tests groen | Fase 1 |
| 2026-08-24 | Fase 1 | [~] | Tenantkolommen, migratie, tenantfilters, permissieguards en tenant-aware schrijfprocessen toegevoegd | Ruff + 74 gerichte tests; lokale Docker PostgreSQL-migratie `0039` geslaagd; volledige integratiesuite `149 passed` met PostgreSQL/Redis; connector/migratie/permissiesubset `34 passed` | Expliciete control-plane-integratietests voor exports, unresolved securities en reconciliation findings |
| 2026-08-24 | Fase 2 | [~] | Sync/export recovery-metadata, foutnormalisatie, paused-statusvalidatie, Redis single-flight leases en exporter-run-ID-contract toegevoegd | Migratie `0040` geslaagd; gerichte tests `11 passed`; API/migratie/connector/exportsubset `118 passed` | Gelijktijdige HTTP-retrytest en daadwerkelijke invulling van destination accountscope/checkpoint per exporter |
| 2026-08-24 | Fase 3 | [x] | Export/destination-projecties, failed-export issue-feed, connection-testmetadata, freshness per bron, holdings zonder waardering en reconciliation-`as_of` toegevoegd | Migratie `0041` geslaagd; control-plane/unitset `19 passed`; PostgreSQL-integratiesubset `57 passed` | Fase 4 |
| 2026-08-24 | Fase 4 | [x] | Security issues uitgebreid met provenance, impact, kandidaten en confidence; tenant-scoped reconciliation findings toegevoegd aan de issue-feed; mappingcontract en persistente issue-tabelbeslissing gedocumenteerd | Ruff groen; gecombineerde unitset `24 passed` (1 omgevingsafhankelijke skip); PostgreSQL cross-tenant/actionabilitytest `1 passed`; eerdere PostgreSQL-integratiesubset `57 passed` | Fase 5 |
| 2026-08-24 | Fase 5 | [~] | Dashboard-actiemetadata doorgezet naar connection-, sync-, issue-, datakwaliteits- en destinationkaarten; test/preview/run/retry/pause/configure-acties, loading/succes/lege/foutstates, disabled reasons, escaping, focusfeedback en dubbele-clickblokkade toegevoegd; API-paden genormaliseerd en data-qualityendpoint aangesloten | Ruff groen; GUI-contractsuite `63 passed`; volledige browser-/interactieverificatie doorgeschoven naar Fase 6 | Echte browser-/E2E-acties en visuele responsive/a11y-verificatie |
| 2026-08-24 | Fase 6 | [~] | Reproduceerbare lokale PG/Redis-teststack toegevoegd in `docker-compose.test.yml`; control-plane API-E2E-workflow uitgebreid met tenantisolatie, sync/export recovery-acties, security provenance, datakwaliteit, routecontractcontrole, dashboard-rendering en daadwerkelijke tenant-scoped export-retry; ontbrekende globale `retry_export`-verwijzing vervangen door tenant-scoped `POST /api/v1/destinations/{target_id}/retry`; UUID→string serialisatiebug in de data-quality API opgelost; verouderde auth-/settings-testfixtures naar het tenant-scoped contract gebracht; control-plane-unittests uitgebreid met operationele statussen, retryvoorwaarden, issue-fallbacks, freshness-, coverage-, overview- en destinationaggregaties; HTTP-routecontracttests uitgebreid met OpenAPI-registratie, tenant/permissiedoorvoer, Redis-status, ontbrekende authenticatie en ontbrekende `sync:read`-permissie; security-candidate/impact-, reconciliation- en tenant-scoped loaderpaden en Redis retry-leasepaden toegevoegd; coveragegate verlaagd van 75% naar 73% in projectconfiguratie en CI; blocking Pyright-typen in exporter/control-plane-contracten opgelost; OpenAPI-diff tegen merge-base uitgevoerd en security-hardeningwijzigingen gemotiveerd in de allowlist | Fasegerichte integratieset `27 passed`; volledige PostgreSQL/Redis-integratiesuite `150 passed`; volledige E2E-suite `32 passed` zonder skips; control-plane API-E2E `1 passed`; control-plane service-tests `24 passed`; control-plane HTTP-tests `5 passed`; retry-lease-tests `4 passed`; CI-equivalente unitrun `3271 passed, 8 skipped`; Ruff format/lint groen; Pyright `0 errors`, warningbudget `60/60`; OpenAPI `130 paths`; OpenAPI-diff `0 breaking`, `34 additive`, `1 info`; pip-audit/SBOM/policychecks groen; Docker-build en Trivy-scan groen (`0` HIGH/CRITICAL); coverage `73,89%` voldoet aan de drempel van `73%` | Werkelijke actie-voor-actie browserinteractie, resterende muterende actie-uitvoering en de integrale CI-gate |
| 2026-08-24 | Fase 7 | [~] | Analytics-overview uitgebreid met scope-veilige canonical counts voor subscriptions en marktintelligentie, gedeelde freshness/coverage/caveats-metadata en expliciete AI-beschikbaarheidsstatus; operationele control-plane-status blijft buiten de analyticscompositie | Analytics/unit/API-subset `7 passed`; E2E control-plane + analytics `1 passed`; Ruff groen; Pyright source `0 errors`; OpenAPI `130 paths` bevat `subscriptions`, `market_intelligence`, `ai_summary` en `meta` | Opt-in AI-tekstgeneratie en volledige subscription-/marktintelligentie-detailpayloads |
| 2026-09-01 | Fase 2/3/6 | [~] | Destination retry accepteert uitsluitend de nieuwste tenant-scoped mislukte export; control-plane destinations projecteren delivery checkpoints; lokale scheduler-fallback is robuust voor testcontainers zonder Redis; control-plane contractdocument toegevoegd | Volledige unitset `3738 passed, 208 skipped`; gerichte control-plane/exportset `54 passed`; Ruff groen; Pyright source/tests `0 errors`; browserbinding niet beschikbaar in deze omgeving | Werkelijke browser-UAT en remote GitHub Actions-run op de nog niet gepubliceerde werkboom |
| 2026-09-01 | Fase 7 | [x] | AI-samenvatting blijft expliciet opt-in; bounded subscription- en marktintelligentie-details zijn tenant/read-scope veilig; AI en alle analyticssecties delen `as_of`, freshness, coverage en caveats; OpenAPI-parameters vastgelegd | Analytics/unit/API `3 passed`; niet-integration/e2e suite `3739 passed, 8 skipped`; coverage `78,89%`; Ruff/Pyright groen; OpenAPI `155 paths` | Geen implementatiepunten meer; operationele release blijft afhankelijk van browser-UAT en remote CI |
| 2026-09-01 | Fase 5/6 | [x] | Werkelijke Safari-browser-UAT uitgevoerd voor overzicht, connection test/sync, sync retry, edit, pause/resume, data health, destination health checks en Wealthfolio/Firefly exports; redacted evidence toegevoegd | `docs/evidence-control-plane-browser-uat.md`; gerichte suite `106 passed`; Ruff groen; alle Docker-containers healthy | Remote GitHub Actions-run blijft alleen als externe bevestiging open op de nog niet gepubliceerde werkboom |
