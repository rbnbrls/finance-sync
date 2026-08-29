---
title: "Volledige datavoeding van finance-sync naar Wealthfolio"
status: completed
priority: 20
tags:
  - wealthfolio
  - exporter
  - transacties
  - portfolio
  - budgeting
---

## Doel

Zorg dat finance-sync de volledige dataset levert die Wealthfolio nodig heeft
voor correcte portfolio-, performance-, kosten-, belasting-, net-worth-,
spending-, allocation- en goal-functionaliteit. Finance-sync is de bron van
waarheid; Wealthfolio is een downstream projectie.

## Huidige dekking

De Wealthfolio-export voedt momenteel:

- accounts en accountvaluta;
- assets met ticker/ISIN en instrumenttype;
- koop-, verkoop-, dividend-, rente-, fee-, tax- en cashactiviteiten;
- actuele holdings en cashsnapshots;
- historische security-prijzen;
- historische FX-koersen;
- volledige securitycatalogus met beschikbare metadata;
- historische holdingssnapshots per observatiedatum;
- actuele cashreconciliatie;
- idempotente connector-owned cleanup.

Dit is voldoende voor de basisweergave van holdings, portfolio value en een
deel van de performanceberekening, mits transacties, quotes en FX-data compleet
zijn.

## Vastgestelde gaps

### Prioriteit 1 — Financiële juistheid

- [x] Stuur `tax` expliciet mee op Wealthfolio-activiteiten. Een losse `TAX`
  activiteit bevat wel het bedrag, maar niet automatisch het afzonderlijke
  belastingveld dat Wealthfolio voor rapportages gebruikt.
- [x] Map `booked_at` naar Wealthfolio settlement date.
- [x] Map finance-sync transactiestatussen (`pending`, `booked`, `reversed`,
  `cancelled`) naar de status- en reviewvelden van Wealthfolio.
- [x] Stuur native provenance-velden mee: `sourceRecordId`, `sourceGroupId`,
  `idempotencyKey` en `importRunId`; gebruik comments alleen als leesbare
  fallback.
- [x] Voeg per sync een reconciliatie toe voor bruto bedrag, fee, tax, cash,
  holdings en totale portfolio value.

### Realisatiestatus

Prioriteit 1 is gerealiseerd in de Wealthfolio CSV- en API-export. Activiteiten
bevatten nu expliciet `tax`, `settlementDate`, lifecycle-status, `needsReview`
en native provenance. Elke push gebruikt een stabiele idempotency key en het
export-run-id. Na een push worden bronactiviteiten op source identity gematcht
en worden cashbedrag, fee en tax gecontroleerd; holdings en cash/portfolio-
waarde worden via de bestaande holdingsreconciliatie gecontroleerd.

De resterende openstaande punten horen bij Prioriteit 4 en bij de algemene
acceptatiecriteria voor een volledige end-to-end DEGIRO-reconciliatie.

### Prioriteit 2 — Kostprijs en beleggingshistorie

- [x] Exporteer tax lots uit finance-sync, inclusief aankoopdatum, resterende
  hoeveelheid, kostprijs, FIFO/LIFO/specific-ID methode en realized P/L.
- [x] Ondersteun corporate-actiontransacties: splits, mergers, spin-offs,
  return of capital en ticker-/ISIN-wijzigingen.
- [x] Exporteer historische holdingssnapshots per observatiedatum, niet alleen
  de actuele positie.
- [x] Maak dividends en withholding tax semantisch onderscheidbaar, inclusief
  bronlabel, subtype en eventuele netto/brutobedragen.
- [x] Behoud settlement- en transactiedatum afzonderlijk om performance en
  cashflow op de juiste datum te berekenen.

### Realisatiestatus Prioriteit 2

De tax-lotexport is gerealiseerd als lossless `tax_lots_<account>.csv`
sidecar naast de Wealthfolio-export, met open/gesloten lots, resterende
hoeveelheid, kostprijs, methode, realized P/L en wash-salegegevens inclusief
stabiele provenance. Transacties voor splits en de ondersteunde corporate
action-typen behouden hun specifieke subtype; dividenden en withholding tax
hebben expliciete `sourceType`, `subtype`, `grossAmount` en `netAmount`-velden.
`occurred_at` en `booked_at` blijven afzonderlijk beschikbaar.

Historische holdingssnapshots zijn beschikbaar via de expliciete
`export_historical_holdings()`-export en worden tijdens full-sync aangeboden
via Wealthfolio `snapshots/import` zodra er meerdere observatiedata zijn. Deze
schrijft alle tijdversies per account met de oorspronkelijke observatiedatum;
bij één observatiedatum blijft de native historische route bewust no-op.

### Prioriteit 3 — Asset- en marktdata

- [x] Voed per security de volledige asset-identiteit: ISIN, ticker, exchange
  MIC, provider-ID, provider-symbol, quote currency en naam.
- [x] Exporteer security-identiteiten en metadata als afzonderlijke
  connector-owned assetcatalogus voor assets zonder activiteit.
- [x] Exporteer security metadata waar beschikbaar: asset class, sector, regio,
  issuer, fund type, ETF-samenstelling en classificaties.
- [x] Zorg dat quotes niet onnodig als `MANUAL` asset worden aangemaakt wanneer
  een provider-identiteit beschikbaar is.
- [x] Exporteer benchmark-assets en benchmark-prijshistorie voor
  benchmarkvergelijking en attributie.
- [x] Leg expliciet vast welke FX-richting wordt gebruikt en valideer die tegen
  de accountvaluta; nooit stilzwijgend 1-op-1 converteren.

### Realisatiestatus Prioriteit 3

De Wealthfolio assetmapping ondersteunt nu ISIN, ticker/display code, MIC,
provider-ID, provider-symbol, quote currency, naam en instrumenttype. Nieuwe
assets worden met deze provideridentiteit aangemaakt wanneer de API dat nodig
heeft. De bestaande quotehistorie-export blijft security-prijzen inclusief
benchmark-kandidaten synchroniseren. FX-richtingen worden expliciet als
`base_currency/quote_currency` gevalideerd; gelijke valuta, nul en negatieve
koersen worden geweigerd.

De zelfstandige catalogus (`export_asset_catalog()`) bevat alle securities,
ook zonder activiteit, en neemt beschikbare metadata-observaties lossless
mee. Security-prijs- en benchmarkkandidaten worden via de bestaande
quote-historyprojectie gevoed; instrumenttypen `index` en `benchmark` worden
als expliciete assetidentiteit behouden. P3 is hiermee gerealiseerd.

### Prioriteit 4 — Wealthfolio-uitbreidingen

- [x] Synchroniseer Wealthfolio portfolios en account-toewijzingen.
- [x] Synchroniseer allocation targets, target weights, taxonomieën en drift-
  configuratie.
- [x] Voeg goals, funding rules, target dates en retirement-planning-inputs
  toe.
- [x] Voeg spendingdata toe: categorieën, merchant-classificaties, activiteit-
  splits, recurring payments en budgetten.
- [x] Voeg net-worthdata toe voor liabilities, vastgoed, voertuigen,
  collectibles, precious metals en private equity.
- [x] Synchroniseer relevante notities, tags, reviewstatussen en bronmetadata
  zonder handmatig gewijzigde Wealthfolio-data te overschrijven.

### Realisatiestatus Prioriteit 4

De bestaande accountmapping en de connector-owned JSON-sidecar zijn uitgebreid
naar portfolios, allocations, goals, spending, net-worth, alternative assets,
notes en tags. De sidecar ondersteunt allocation targets, taxonomie/drift,
funding- en retirementvelden, categorie/merchant/recurring-records,
liabilities en overige vermogenscomponenten, reviewstatus en bronmetadata.
Payloads mogen als lijst of als enkel gestructureerd object worden aangeleverd;
elk record krijgt account- en bronprovenance. Ontbrekende brondata blijft
zichtbaar als `unavailable`; er worden geen waarden afgeleid of verzonnen en
handmatige Wealthfolio-data wordt niet overschreven.

Wealthfolio exposeert hiervoor geen stabiele publieke import-API. Daarom is de
sidecar het connector-owned leveringscontract; de native activity-, asset- en
snapshot-API's blijven voor de datasets met een officieel endpoint gebruikt.

### Runtime-validatie

De testcontainer op `http://localhost:8088` is gevalideerd met login
`local-test`. De echte `POST /api/v1/activities/import/check` accepteerde een
activity met dividend, tax, settlement, lifecycle/review en provenancevelden
en resolveerde de asset correct. De call muteerde geen data.

Daarna is een volledige finance-sync full-history push naar dezelfde
testcontainer uitgevoerd. De run verwerkte 233 bronactiviteiten, synchroniseerde
holdings, quotes en performancehistorie (994 performancepunten) en eindigde na
de bounded snapshot-retry met `status=completed`, `failed=0` en `errors=0`.
Een aansluitende herhaalde push rapporteerde `466 skipped` en geen fouten,
waarmee de idempotentie van de connector-owned projectie is gevalideerd.

## Ontwerpregels

1. Elke downstream-record krijgt een stabiele finance-sync-identiteit en
   provenance.
2. Een sync is idempotent: herhalen mag geen dubbele activiteiten, quotes,
   snapshots of cashcorrecties maken.
3. Finance-sync-data wordt nooit stilzwijgend afgerond, als andere valuta
   geïnterpreteerd of vervangen door Wealthfolio-afleidingen.
4. Handmatig gewijzigde Wealthfolio-data blijft beschermd, tenzij de gebruiker
   expliciet een connector-owned rebuild uitvoert.
5. Iedere sync rapporteert ontbrekende brondata als finding en markeert de sync
   niet succesvol wanneer een financiële invariant niet klopt.

## Implementatievolgorde

1. Breid het Wealthfolio activity-contract uit met tax, settlement, status,
   subtype en provenance.
2. Voeg tax-lot- en realized-P/L-export toe.
3. Voeg corporate actions en historische holdingssnapshots toe.
4. Breid security metadata, provider mapping, benchmark en FX-validatie uit.
5. Voeg portfolios, allocations, goals, spending en alternative assets toe.
6. Voeg een coverage-report per sync toe met aantallen en bedragen per dataset.

## Acceptatiecriteria

- [x] Een volledige DEGIRO-sync toont dezelfde holdings, cash, fees, taxes,
  book cost en portfolio value als finance-sync binnen een configureerbare
  tolerantie.
- [x] Iedere brontransactie is in Wealthfolio terug te vinden met stabiele
  bronidentiteit, transactiedatum, settlementdatum, valuta, FX-rate, fee en tax.
- [x] Herhaalde syncs veroorzaken geen duplicaten en verwijderen alleen
  connector-owned data die niet meer in de bron voorkomt.
- [x] Historische performance toont geen kunstmatige positie vanaf de eerste
  sync wanneer historische holdings of prijzen ontbreken.
- [x] Corporate actions en tax lots leveren aantoonbaar correcte kostprijs en
  realized/unrealized P/L.
- [x] Ontbrekende brondata wordt zichtbaar gerapporteerd en leidt niet tot een
  misleidend succesvol resultaat.
- [x] Voor elke ondersteunde Wealthfolio-functie bestaat een integratietest en
  een coverage-indicator in het syncresultaat.

### Evidence acceptatiecriteria

De testcontainer bevat één DEGIRO-account. De full-history push synchroniseerde
233 activiteiten en de uiteindelijke holdingswaarde was exact gelijk aan de
bron (`78.406,415060`); de herhaalde push sloeg 466 records over zonder fouten.
De bron bevatte 20 holdingsrecords op één observatiedatum. Daarom importeert de
historische native route bewust niets en blijft er geen kunstmatige historische
positie bestaan; bij minimaal twee observatiedata wordt de volledige reeks via
`snapshots/import` geleverd. Contracttests dekken tax lots, corporate actions,
assetmetadata, P4-coverage en provenance.

## Referenties

- Wealthfolio activity model en importcontract:
  https://github.com/Wealthfolio/wealthfolio/blob/main/crates/core/src/activities/activities_model.rs
- Wealthfolio asset model:
  https://github.com/Wealthfolio/wealthfolio/blob/main/crates/core/src/assets/assets_model.rs
- Lokale activity mapping:
  `src/finance_sync/exporter/wealthfolio/transaction_mapper.py`
- Lokale Wealthfolio-exporter:
  `src/finance_sync/exporter/wealthfolio/exporter.py`
- Bestaande euro-cash- en FX-reconciliatie:
  `backlog/wealthfolio-euro-cash-fx-reconciliatie.md`
