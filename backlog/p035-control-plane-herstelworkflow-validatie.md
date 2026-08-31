---
title: "Valideer de control-plane herstelworkflow end-to-end"
status: in-progress
priority: 35
---

# Implementatieplan — resterende control-plane punten

## Implementatiestatus

De code- en contractrestpunten uit dit plan zijn gerealiseerd. Issues blijven
vooralsnog afgeleide projecties; er is geen behoefte aan acknowledge, snooze of
assignment vastgesteld, dus een persistente `control_plane_issues`-tabel is
niet toegevoegd. De lokale control-plane-, API-, GUI- en analyticschecks zijn
geslaagd. Een echte browserruntime was in de uitvoeromgeving niet beschikbaar;
de interactieve browsergate blijft daarom een expliciete deployment/UAT-gate.

## Openstaande punten

- [ ] Voer de browserworkflow uit met keyboard-only navigatie en controleer
  acties, loading/error states, tenantcontext en status na retry.
- [ ] Leg de UAT vast als redacted artifact met commit, omgeving, scenario-
  resultaten en timestamp; neem geen credentials of financiële waarden op.
- [ ] Herhaal de bestaande contract-, API-, integratie- en E2E-gates na fixes
  en link de artifacts in dit bestand.

`status: done` mag alleen worden gezet wanneer deze punten en de Definition of
Done van het hoofdplan aantoonbaar zijn afgerond.

## Doel en uitgangssituatie

Maak de control plane production-ready en aantoonbaar volledig volgens de
Definition of Done in
[`control-plane-datastroom-beheeromgeving.md`](./control-plane-datastroom-beheeromgeving.md).

De bestaande basis blijft het uitgangspunt: `ControlPlaneService`, de
control-plane schemas en route, sync-run detail/retry, export-run routes,
`DataQualityService`, de uniforme action-catalogus en het bestaande HTMX/
Jinja-dashboard bestaan al gedeeltelijk. Dit plan beschrijft alleen de
resterende hardening, ontbrekende projecties, UI-acties en verificatie.

## Scopebesluit

- Geen nieuwe connectoren, budgetfunctionaliteit of analytische domeinen.
- Alle reads en mutaties blijven tenant-scoped en permission-scoped.
- Issues blijven afgeleide projecties zolang acknowledge, snooze of assignment
  niet nodig is. In werkpakket 4 wordt dit besluit opnieuw getoetst; alleen bij
  een concrete productbehoefte volgt een migratie voor
  `control_plane_issues`.
- Geen nieuwe routes wanneer een bestaande route veilig kan worden gebruikt.
  Contractafwijkingen worden wel expliciet vastgelegd in schema’s, OpenAPI en
  tests.

## Werkpakketten en volgorde

### 1. Contractinventarisatie en veilige basis

**Doel:** één actuele matrix maken van modellen, routes, permissies, acties en
tenantfilters voordat code wordt aangepast.

**Te inspecteren/aanpassen:**

- `src/finance_sync/services/control_plane.py`
- `src/finance_sync/schemas/control_plane.py`
- `src/finance_sync/services/control_plane_actions.py`
- `src/finance_sync/api/v1/sync_runs.py`
- `src/finance_sync/api/v1/exporters.py`
- `src/finance_sync/api/v1/securities.py`
- `src/finance_sync/api/v1/reconciliation.py`
- `src/finance_sync/models/credential.py`
- `src/finance_sync/models/export_target.py`
- `src/finance_sync/exporter/models.py`

**Implementatie:**

1. Leg per route vast: tenantbron, vereiste permissie, resource ownership,
   mutatiegedrag, retry/idempotentie en foutcontract.
2. Maak de action-catalogus de enige bron voor action metadata. Voeg ontbrekende
   routes alleen toe als de bestaande API geen veilige actie biedt.
3. Normaliseer gebruikersstatussen en foutcategorieën naar het afgesproken
   contract; behoud provider- en stacktrace-informatie uitsluitend in logs.
4. Controleer of `last_error_category` en connection-testvelden als modelvelden
   bestaan. Als ze ontbreken, voeg een gerichte migratie en schrijfpaden toe;
   gebruik geen `getattr` als permanente contractoplossing.

**Klaar wanneer:** de contractmatrix klopt met de feitelijke routes en iedere
actie uit een overview naar een bestaande, geautoriseerde API-operatie wijst.

### 2. Backendaggregatie: exports, destinations, freshness en tijdssemantiek

**Doel:** de overview vormt één consistente tenant-scoped projectie van de
volledige datastroom.

**Implementatie:**

1. Breid `ControlPlaneDestination` uit waar nodig met accountscope,
   exportstatus, laatste exportfout, laatste exporttijd, failed count en alle
   acties: testen, preview, uitvoeren, retry, pauzeren en configureren.
2. Projecteer `ExportRun` alleen via `tenant_id` en `target_id`; voeg mislukte
   runs toe aan de centrale issue-feed. Gebruik per destination exact één
   retryactie, met disabled state wanneer de bestemming niet actief is of de
   gebruiker geen `destinations:write` heeft.
3. Controleer dat retry naar de bestaande exporter-route gaat, de originele
   run voor audit bewaart, cursors/idempotentie respecteert en geen nieuwe
   destination-configuratie vereist.
4. Breid freshness uit met ingestie versus marktdata, holdings zonder
   waardering, freshness per bron én categorie, en consistente classificatie
   `fresh`, `stale`, `partial`, `unavailable`. Breid coverage uit naar
   provider/resource waar de bestaande modellen dat toelaten.
5. Definieer `as_of` als de laatste betrouwbare onderliggende domeintimestamp
   (sync, export, enrichment, destination health en reconciliation), niet als
   `generated_at`. Valideer dat ontbrekende timestamps niet als actuele data
   worden geïnterpreteerd.
6. Pas statusregels aan zodat export failures en reconciliation findings de
   overviewstatus en summary aantoonbaar beïnvloeden, zonder hetzelfde issue
   dubbel te tellen.

**Verificatie:** unit tests voor aggregatie, failed-export deduplicatie,
freshnessclassificatie, `as_of` en overview-status; API-tests met twee tenants.

### 3. Tenantisolatie, autorisatie en veilige herstelmutaties

**Doel:** geen enkele control-plane read of mutatie kan data of acties van een
andere tenant bereiken.

**Implementatie:**

1. Verifieer en herstel tenantfilters in `GET /api/v1/sync-runs`, sync detail,
   sync retry, `GET /api/v1/exporters/runs`, export detail en export retry.
   Gebruik bij ontbrekende ownership dezelfde 404-respons als voor niet
   bestaande resources.
2. Voeg expliciete auth- en permission-dependencies toe aan unresolved,
   resolve, map en audit-log routes: `securities:read` voor reads en
   `securities:write` voor mapping. Maak het contract gelijk aan
   `PUT /api/v1/securities/{security_id}/map`, of documenteer de bestaande
   afwijking in OpenAPI en test die consequent.
3. Scope security candidates, impactcounts, reconciliation findings,
   connections, destinations en exports op de authenticated tenant. Gebruik
   geen provider-only query als tenantfilter ontbreekt.
4. Maak acties server-side disabled of foutgevoelig voor read-only users,
   ontbrekende permissies, gepauzeerde connections en niet-actieve
   destinations; vertrouw niet op alleen disabled buttons in de GUI.
5. Behoud gesaneerde foutmeldingen in sync/export detail en test op secrets,
   tokens, credentials, stacktraces en providergevoelige payloads.
6. Gebruik de bestaande `retry_lease` voor sync/export. Voeg, waar nodig,
   database- of statechecks toe zodat dubbele clicks en parallelle retries
   hetzelfde veilige resultaat geven. Verifieer dit met PostgreSQL en Redis.

**Verificatie:** cross-tenant matrix voor connections, syncs, destinations,
exports, unresolved securities en reconciliation; permission matrix; parallelle
retrytests; security-redactiontests.

### 4. Security mapping en datakwaliteit compleet maken

**Doel:** ieder datakwaliteitsprobleem heeft context, impact en één bruikbare
vervolgactie.

**Implementatie:**

1. Laat identity resolution candidates en confidence score rechtstreeks in de
   control-plane issueprojectie landen; houd de bronrecord en connectorherkomst
   zichtbaar.
2. Maak impactcounts correct per unresolved record: geraakte holdings en
   transacties, niet een providerbrede telling die meerdere issues hetzelfde
   laat lijken.
3. Laat een succesvolle security mapping de onderliggende unresolved row
   oplossen en bewijs dat de issue bij de eerstvolgende overview verdwenen is
   of expliciet `confirmed` is.
4. Voeg reconciliatieproblemen toe aan het actiecentrum met een tenant-scoped
   detailactie. Integreer bestaande duplicate-, missing-, mismatch- en
   provenancegegevens in `DataQualityService` en de dashboardprojectie.
5. Voeg coverage per provider/resource, historische bronrecords en impactdetails
   toe waar de bestaande data beschikbaar is; label ontbrekende historie als
   niet beschikbaar.
6. Beslis aan het eind van dit werkpakket of acknowledge, snooze of assignment
   werkelijk nodig is. Alleen dan: introduceer een tenant-scoped
   `control_plane_issues`-tabel met fingerprint, status, severity, timestamps
   en payload, plus migratie, upsert/reconciliation-job en auditlog. Zonder die
   productbehoefte blijven issues afgeleid.

**Verificatie:** mapping-flow van unresolved naar resolved, candidate-
confidencecontract, reconciliation action/API, duplicate/provenance/coverage
tests en een expliciete beslissingstest voor persistente issues.

### 5. Dashboard en herstelacties

**Doel:** de gebruiker kan de volledige herstelworkflow vanuit één scherm
uitvoeren en ziet na elke actie het nieuwe resultaat.

**Bestand:** `src/finance_sync/templates/dashboard.html`; voeg alleen kleine
herbruikbare template/static helpers toe als de inline structuur dat vereist.

**Implementatie:**

1. Maak statusheader en actiecentrum volledig: severity, oorzaak, impact,
   concrete actie, loading-, success- en errorstate, en opnieuw laden van de
   issue-feed.
2. Breid connection-kaarten uit met testen, nu synchroniseren, bewerken,
   pauzeren en details; toon testresultaat, foutcategorie, laatste fout en
   volgende geplande run.
3. Breid destination-kaarten uit met testen, preview, uitvoeren, retry,
   pauzeren en configureren; toon accountscope, laatste exportstatus/fout en
   volgende run.
4. Integreer sync-run detail, statusfilters en retry met alle labels: Bezig,
   Voltooid, Mislukt, Gedeeltelijk, Overgeslagen en Geannuleerd.
5. Toon in datakwaliteit unresolved securities, stale prices, ontbrekende
   waardering, coverage, reconciliation, duplicates, provenance en impact.
6. Zorg dat één issue niet meerdere concurrerende acties toont; de backend is
   leidend voor enabled/disabled en disabled reason.
7. Behoud XSS-safe rendering via bestaande escaping, voeg keyboard focus,
   `aria-live`, `aria-busy`, correcte buttonlabels, foutmeldingen en mobiele
   layout toe.

**Verificatie:** browser/interactietests voor iedere actie, lege/laad/foutstates,
retry na reload, keyboard-only navigatie, screenreader-asserties, XSS-payloads
en mobiele viewport.

### 6. Analytics-contract harmoniseren

**Doel:** analytics consumeren dezelfde canonieke datasetmetadata zonder een
nieuw operationeel statusmodel te introduceren.

**Bestanden:**

- `src/finance_sync/services/analytics_overview.py`
- `src/finance_sync/schemas/analytics.py`
- `src/finance_sync/api/v1/analytics.py`
- de bestaande services voor subscriptions, marktintelligentie en AI summary

**Implementatie:**

1. Controleer dat analytics-overview subscriptions, marktintelligentie en
   AI-samenvattingen naast portfolio, performance en cashflow projecteert.
2. Laat iedere sectie hetzelfde metadata-contract gebruiken: `as_of`,
   freshness, coverage en caveats; onbekende of niet-geconfigureerde bronnen
   krijgen expliciet `unknown`/`unavailable`.
3. Maak de globale analyticsmetadata deterministisch op basis van de secties
   en documenteer de scopebeperkingen.

**Verificatie:** schema-, service- en API-tests voor aanwezige, lege, stale en
niet-geconfigureerde secties; regressietest dat analytics geen control-plane
issues of operationele statussen dupliceert.

### 7. End-to-end bewijs en releasegate

Voer na werkpakket 1–6 de volledige workflow uit:

```text
verbinding toevoegen → testen → eerste sync → sync-fout → detail → retry
→ unresolved security → candidates/confidence → security mappen
→ freshness/coverage controleren → destination testen
→ export laten falen → export retry → gezonde overview
```

Gebruik een dataset met minstens twee tenants, twee connections, één
gepauzeerde connection, één niet-actieve destination, unresolved securities,
stale quotes, reconciliation findings en een failed export. Voer uit:

- unit- en contracttests;
- API-, tenant- en permissiontests;
- PostgreSQL/Redis-integratietests voor leases en idempotentie;
- GUI/browser- en accessibilitytests;
- OpenAPI/schema-diff en bestaande release-/securitygates;
- coveragegate en regressiesuite.

De release is pas geslaagd als alle acties werkelijk uitvoerbaar zijn, iedere
issue precies één vervolgstap heeft, geen cross-tenant data lekt, retries
idempotent zijn en de laatste overview de hersteltoestand correct weergeeft.

## Aanbevolen commit-/oplevereenheden

1. `control-plane-contracts-and-aggregation`
2. `tenant-permissions-and-safe-retries`
3. `security-and-data-quality-projection`
4. `dashboard-recovery-actions`
5. `analytics-metadata-contract`
6. `control-plane-e2e-verification`

Elke eenheid bevat code, gerichte tests en bijgewerkte OpenAPI/contract-
evidence; pas na werkpakket 7 wordt het bronbacklog als volledig gereed
gemarkeerd.
