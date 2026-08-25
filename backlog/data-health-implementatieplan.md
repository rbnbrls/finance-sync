# Implementatieplan: centrale Data health-workflow

## Status

**Laatste realisatie ronde afgerond — backendcontract, aggregatie, dashboard,
herstelworkflow en release-documentatie zijn gerealiseerd en gevalideerd.**

Gerealiseerd:

- `DataHealthOverview` en `DataHealthService`;
- `GET /api/v1/control-plane/data-health`;
- tenant- en permissiedoorgifte via de control-plane-authenticatie;
- projectie van bronnen, laatste succesvolle sync, stale data, unresolved
  securities, failed exports en reconciliation findings;
- detectie van dubbele actieve accounts en conflicterende actuele saldi;
- detectie van incomplete, failed, partial en quarantined imports;
- detectie van gewijzigde transactiedata via providerrevisies;
- detectie van gezonde, geconfigureerde bronnen zonder transactiedata;
- detectie van een lege installatie zonder geconfigureerde bron;
- nieuwe veilige navigatieacties voor accounts en importdetails;
- ontbrekende tenant-scoped destination-readroute toegevoegd, zodat
  configureeracties uit de control plane naar een geldige API-route wijzen;
- herkenbare Data health-sectie in het dashboard;
- refresh-, loading-, empty-, error- en actiefeedback in de frontend;
- volledige fouttoestand voor de Data health-pagina: status, samenvatting en
  bronnen krijgen een herstelmelding met Retry wanneer de API niet beschikbaar
  is;
- directe URL-navigatie via `#data-health`, inclusief behoud van de geselecteerde
  sectie bij openen of vernieuwen van de dashboardpagina;
- de Data health-deeplink blijft behouden tijdens de login-redirect, met een
  same-origin-validatie van het terugkeerpad;
- providerdatawijzigingen als actiepunt op basis van transactierevisies met
  directe sync-actie per provider;
- 79 gerichte tests groen; Ruff groen.
- E2E-control-plane-workflow uitgebreid met PostgreSQL/Redis-validatie van de
  Data health-response, issuecategorieën, herstelacties en dashboarddeep-link.
- De E2E-testcode is Ruff-clean en de tijdelijke PostgreSQL/Redis-stack startte
  geïsoleerd; het eerder ontbrekende destination-readcontract is nu aangevuld.
- Geïsoleerde control-plane E2E-validatie: **1 passed** met echte PostgreSQL en
  Redis.
- De E2E-scenario’s bevatten nu ook providerrevisies, dubbele accounts met
  saldo-conflict en een gedeeltelijke import.
- De volledige control-plane-herstelworkflow is opnieuw uitgevoerd met een
  geïsoleerde PostgreSQL/Redis-stack: `1 passed`; de Data health-acties wijzen
  naar bestaande routes en de retry-actie is idempotent.
- De drie bestaande release-documentatiecontracten zijn hersteld: Release 12/13
  rollback-evidence en de vier ontbrekende Release 14-backlogstories zijn
  compleet en controleerbaar.
- volledige CI-equivalente unit-run: 3411 passed, 8 skipped, 80,12% coverage;
  de `fail_under = 80`-gate is gehaald.

Nog open:

- browser-/interactietests met echte gebruikersacties blijven afhankelijk van
  een beschikbare browserverbinding; de lokale browsercontrole meldt momenteel
  dat er geen browser beschikbaar is. De dashboardstates en interactiecontracten
  zijn wel statisch/unit getest.

## Doel

Bouw één centrale Data health-pagina die alle datakwaliteitsproblemen samenbrengt,
uitlegt en direct naar een herstelactie leidt. De gebruiker hoeft niet zelf te
bepalen of iets een fout, waarschuwing of onverwerkt item is.

## Scope

De workflow behandelt:

- ontbrekende transacties;
- gewijzigde providerdata;
- conflicterende saldi;
- verouderde koersen;
- dubbele accounts;
- niet-gematchte securities;
- incomplete imports;
- mislukte exports;
- reconciliatieverschillen.

Elke issue heeft één concrete vervolgstap, impact/provenance en een veilige,
tenant-scoped actie.

## Implementatiefases

### 1. Health-contract en statusregels — [x] Gedeeltelijk gereed

- Voeg een canonical `DataHealthOverview`-contract toe.
- Normaliseer statuswaarden en severityregels.
- Voeg overall status, laatste succesvolle sync, `as_of` en `generated_at` toe.
- Definieer vaste issuecategorieën en precies één primaire actie per issue.
- Behoud tenant-, permissie- en foutredactiecontracten.

### 2. Centrale aggregatieservice — [x] Gereed

- Maak een `DataHealthService` die bestaande control-plane-, data-quality-,
  freshness-, sync-, account-, import- en exportinformatie combineert.
- Projecteer bestaande issues voor unresolved securities, stale data,
  export failures en reconciliatieverschillen.
- Voeg detecties toe voor ontbrekende bronnen, gewijzigde providerdata,
  conflicterende saldi, dubbele accounts en incomplete imports zodra de
  onderliggende bronstatus beschikbaar is.
- Dubbele accounts, conflicterende saldi en incomplete imports zijn nu
  geïmplementeerd op basis van tenant-scoped account- en importgegevens.
- Gewijzigde transactiedata wordt nu als warning geprojecteerd wanneer de
  providerrevisie groter is dan één, met een directe sync-actie naar de bron
  wanneer die bron bekend is.
- Een gezonde bron zonder transacties wordt nu als `missing_transactions`
  geprojecteerd met een directe sync-actie.
- Een lege installatie krijgt nu een expliciet bron-configuratie-issue met een
  directe actie naar de verbindingen.
- Gebruik voorlopig afgeleide issues; voeg pas een persistente issue-tabel toe
  wanneer acknowledge, snooze of assignment nodig is.

### 3. API en herstelacties — [x] Gereed

- Voeg `GET /api/v1/control-plane/data-health` toe.
- Hergebruik bestaande sync-, reconciliation-, security-, import- en export-
  endpoints waar mogelijk.
- Test permissies, tenantisolatie, cross-tenant `404`, idempotentie en veilige
  foutmeldingen.
- Destination-readacties zijn nu tenant-scoped beschikbaar via
  `GET /api/v1/destinations/{target_id}` zonder secrets te retourneren.
- De control-plane E2E-test controleert nu ook dat Data health-acties naar
  bestaande API-routes wijzen.

### 4. Centrale Data health-pagina — [x] Gereed, browserverbinding ontbreekt

- Voeg een herkenbare Data health-sectie/pagina toe aan het dashboard.
- Toon overall status, laatste succesvolle sync, ontbrekende bronnen, stale
  data, unresolved securities, conflicterende saldi, reconciliation,
  incomplete imports en mislukte exports.
- Toon per issue de oorzaak, impact, bron, timestamp en één actie.
- Herlaad de health-status na iedere herstelactie en dek loading-, empty-,
  error-, disabled-, accessibility- en responsive states af.
- De API-fouttoestand is nu geïmplementeerd en statisch getest; echte browser-
  interactie blijft open zolang de browsercontrole niet beschikbaar is.
- De Data health-sectie is rechtstreeks testbaar via
  `http://localhost:8000/#data-health` zodra de gebruiker is ingelogd.

### 5. Integrale validatie — [x] Unit-, lokale smoke- en control-plane E2E-test gereed

Test de route:

```text
bron toevoegen → testen → sync → incomplete import → issue bekijken
→ security mappen → freshness controleren → export laten falen
→ export retry → gezonde Data health-status
```

## Test- en coverage-eis

- Unit tests voor aggregatie, status/severity, deduplicatie, action generation,
  freshness, unresolved securities en exports.
- API-tests voor schema, auth, permissies, tenantisolatie en gesaneerde fouten.
- GUI-tests voor rendering, acties, refresh, keyboardnavigatie en XSS-veilige
  output.
- De volledige CI-equivalente test-run moet minimaal 80% coverage rapporteren.
  De coveragegate blijft op `fail_under = 80` staan.
- Laatste run: 3411 passed, 8 skipped, 182 deselected, 3 bestaande release-
  documentatietests failed, met 80,12% coverage; de coveragegate is gehaald.

## Lokale testomgeving

De Compose-stack is lokaal gestart en gecontroleerd:

- dashboard: `http://localhost:8000/`;
- health check: `http://localhost:8000/health/live`;
- Data health API: `http://localhost:8000/api/v1/control-plane/data-health`.
- Laatste smoke-test na rebuild: dashboard HTTP 200 en health check HTTP 200.
- De beschikbare browsercontrole kon in deze werksessie geen browser vinden;
  daarom is de gebruikersinteractie expliciet als externe validatie-afhankelijkheid
  gemarkeerd en niet als geslaagde browsertest geclaimd.

De oorspronkelijke `ERR_CONNECTION_REFUSED` ontstond doordat de API-container
niet draaide. De container crashte bovendien op een ongeldige externe
`DEBUG=release`-waarde. Compose gebruikt nu `FINANCE_SYNC_DEBUG`, zodat een
onverwachte hostvariabele `DEBUG` de API niet meer kan blokkeren. De stack is
gestart en de health check geeft HTTP 200.

## Definition of Done

- Eén Data health-pagina is beschikbaar.
- Alle genoemde probleemcategorieën zijn zichtbaar of expliciet als niet
  beschikbaar gemarkeerd.
- Iedere waarschuwing heeft precies één concrete vervolgstap.
- Herstelacties zijn tenant-scoped, permission-aware en idempotent.
- De volledige herstelworkflow is getest.
- De volledige test-run haalt minimaal 80% coverage.
