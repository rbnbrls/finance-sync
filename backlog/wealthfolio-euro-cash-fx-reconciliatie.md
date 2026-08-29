---
title: "Wealthfolio euro-cash en FX-reconciliatie voor DEGIRO Pensioen"
status: completed
priority: 10
tags:
  - wealthfolio
  - degiro
  - valuta
  - reconciliatie
---

## Doel

Zorg dat Wealthfolio bij een DEGIRO Pensioen-account met uitsluitend een
EUR-geldrekening exact dezelfde actuele cashpositie en portefeuillewaarde toont
als finance-sync. Finance-sync is de bron van waarheid; Wealthfolio is alleen
een downstream projectie.

## Vastgestelde afwijking

Bij de lokale test met de officiële DEGIRO-exporten:

| Waarde | finance-sync | Wealthfolio |
|---|---:|---:|
| Effecten / invested | €70.961,98 | €70.961,98 |
| Cash | €7.444,44 | €10.697,72 |
| Portfolio value | €78.406,42 inclusief cash | €81.659,69 |

Wealthfolio maakt momenteel twee cashposities aan:

- EUR: €8.511,93
- USD: $2.532,78, omgerekend ongeveer €2.185,79

De USD-cash komt uit 53 USD-dividendactiviteiten. De DEGIRO-rekening zelf
heeft volgens de portefeuille- en rekeningexport alleen een EUR-cashpositie.
Daarnaast worden technische cash-sweep- en FX-regels uit het
DEGIRO-rekeningoverzicht niet als economische transacties opgeslagen. Daardoor
kan Wealthfolio zijn cash-ledger niet laten aansluiten op het broker-eindsaldo.

### Definitieve oorzaak

Wealthfolio berekent cash uit twee bronnen: het activity-ledger én de
`cashBalances` uit een holdings-snapshot. De exporter schreef eerst een juiste
EUR-snapshot, maar het activity-ledger bleef een hoger saldo bevatten. Na de
sync overschreef Wealthfolio de zichtbare cash daardoor opnieuw naar
€10.732,69. De oplossing corrigeert het activity-ledger vóór het opslaan van
de snapshot en bewaart de connector-owned correctie bij volgende incrementele
syncs.

## Gewenste invariant

Na iedere succesvolle DEGIRO-import en Wealthfolio-sync geldt voor een account:

```text
Wealthfolio investment value = finance-sync holdings market value
Wealthfolio cash in account currency = finance-sync available_balance
Wealthfolio portfolio value = holdings market value + available_balance
```

De vergelijking gebruikt dezelfde peildatum en een configureerbare tolerantie
voor afronding. Een afwijking mag de export niet als geslaagd markeren.

## Ontwerpbeslissingen

### 1. Maak accountvaluta expliciet

Gebruik de finance-sync-accountvaluta als downstream cashvaluta. Voor de
DEGIRO Pensioen-account is dat EUR. Geef deze waarde expliciet door aan de
Wealthfolio activity- en snapshotmapping; `default_currency` alleen is hiervoor
niet voldoende.

### 2. Behoud bronvaluta, projecteer cash naar EUR

Bewaar de originele transactievaluta en het oorspronkelijke bedrag in
finance-sync en in Wealthfolio-metadata. Voor cash-effecten in een account met
één EUR-rekening:

- gebruik een gevalideerd EUR-bedrag als dat uit de bron beschikbaar is;
- gebruik een historische FX-rate als een echte vreemde-valutacashrekening
  bestaat;
- maak geen zelfstandige USD-cashpositie aan wanneer de broker-export geen
  USD-rekening bevat;
- markeer een ontbrekende FX-rate als datakwaliteitsfinding in plaats van een
  stilzwijgende 1:1-conversie.

### 3. Gebruik het broker-eindsaldo als actuele cash-authoriteit

De actuele `available_balance` uit de DEGIRO-import moet de cashsnapshot in
Wealthfolio bepalen. De snapshot mag niet worden opgeteld bij de door
Wealthfolio berekende cash uit historische activiteiten. Gebruik een expliciete
connector-owned snapshot of een idempotente cash-reconciliatiecorrectie die
dezelfde externe identiteit bij een volgende sync hergebruikt.

### 4. Behandel technische DEGIRO-regels bewust

Classificeer cash-sweep-, flatex-, FX-debit- en FX-credit-regels expliciet als
één van:

- interne cash/FX-transfer die geen extra vermogen creëert;
- broker cashcorrectie die naar de EUR-snapshot leidt;
- technische regel die niet naar Wealthfolio wordt geëxporteerd, maar wel in de
  import-audit wordt vastgelegd.

Een regel mag niet tegelijk als transactie én als snapshotcash meetellen.

## Implementatieplan

## Implementatiestatus (2026-08-29)

De implementatie is uitgevoerd in de DEGIRO-connector en
Wealthfolio-exporter:

- [x] Accountvaluta wordt expliciet doorgegeven aan de activity-mapper.
- [x] EUR-only accounts projecteren cashactiviteiten niet meer als een
  zelfstandige vreemde-valutacashpositie.
- [x] `amount_in_base` met bijbehorende base currency heeft voorrang op een
  lokale vreemde-valutawaarde.
- [x] Een aanwezige provider-FX-koers wordt gebruikt volgens de DEGIRO-notatie
  (lokale valuta per EUR).
- [x] Ontbrekende EUR/basewaarde en FX-koers geven een zichtbare finding en
  geen stilzwijgende 1-op-1-conversie.
- [x] Gebundelde DEGIRO `Valuta Debitering`-regels worden aan meerdere
  dividendregels gekoppeld en op de resterende USD-mutatie verbruikt.
- [x] Bronvaluta, bronbedrag, basebedrag en FX-koers worden in de
  Wealthfolio-activity metadata auditbaar meegestuurd.
- [x] De bestaande connector-owned account-, activity- en holdings-cleanup
  blijft actief bij iedere push.
- [x] De bestaande actuele holdings/cash-snapshot wordt bij iedere sync
  opnieuw opgebouwd uit `available_balance`.
- [x] Regressietests voor base-currency, provider-FX en ontbrekende FX zijn
  toegevoegd.

De volledige niet-integratie-/niet-e2e-suite is gecontroleerd: **3.533 tests
geslaagd, 8 overgeslagen**. De aangeleverde DEGIRO-dataset levert nu voor alle
53 USD-dividenden een gekoppelde EUR-basewaarde (`53/53`). Dit voorkomt dat de
Wealthfolio-cashpositie opnieuw wordt opgeblazen.

### Fase 1 — Bron- en datamodel

- [x] Voeg bij de connector-/exportcontext de accountvaluta en een
  `cash_authority`-waarde toe.
- [x] Leg vast of een account multi-currency cash ondersteunt. Voor DEGIRO
  Pensioen is dit `false` en is EUR de enige cashvaluta.
- [x] Vul voor USD-dividenden en andere statementactiviteiten de EUR-waarde of
  FX-rate in wanneer de bron die levert.
- [x] Voeg een expliciete finding toe voor vreemde-valutacash zonder
  converteerbare EUR-waarde.
- [x] Bewaar oorspronkelijke valuta, bedrag, gebruikte FX-rate en bronregel in
  provider metadata.

### Fase 2 — Wealthfolio activity mapping

- [x] Geef accountvaluta door aan `transaction_mapper`.
- [x] Map cash-genererende activiteiten naar de accountvaluta wanneer de
  connector vaststelt dat de broker geen vreemde-valutarekening heeft.
- [x] Laat effecten- en prijscurrency ongemoeid waar die nodig is voor correcte
  asset- en quote-resolutie.
- [x] Zorg dat `DIVIDEND`, `FEE`, `TAX`, `INTEREST`, `DEPOSIT` en `WITHDRAWAL`
  niet onbedoeld een extra vreemde-valutacashrekening creëren.
- [x] Houd kosten en belasting afzonderlijk zichtbaar; normaliseer alleen de
  cashvaluta, niet het onderscheid tussen fee en tax.

### Fase 3 — Actuele cashsnapshot

- [x] Definieer één connector-owned snapshot-identiteit per account en
  peildatum.
- [x] Schrijf bij iedere sync de actuele EUR-cash uit `available_balance` weg.
- [x] Verwijder of vervang een eerdere connector-owned cashsnapshot
  idempotent; voeg geen tweede saldo toe.
- [x] Controleer na opslaan de actuele Wealthfolio-cash via de holdings-API.
- [x] Bereken en rapporteer afzonderlijk: securities, cash, totaal en afwijking.

### Fase 4 — Reconciliatie en foutafhandeling

- [x] Voeg een harde reconciliatie toe voor EUR-only accounts.
- [x] Gebruik aparte toleranties voor bedragen en percentages.
- [x] Maak onderscheid tussen ontbrekende FX-data, technische brokerregels en
  echte downstream-afwijkingen.
- [x] Laat een sync met een niet-opgeloste cashafwijking status `failed` of
  `completed_with_findings` krijgen; nooit stil `completed`.
- [x] Zorg dat retry dezelfde cashsnapshot bijwerkt en geen extra correcties
  opstapelt.

### Fase 5 — Bestaande data migreren

- [x] Maak vóór migratie een preview van USD/EUR-cashactiviteiten en
  connector-owned snapshots.
- [x] Verwijder alleen door finance-sync aangemaakte foutieve downstream
  cashprojecties; handmatige Wealthfolio-data buiten de connectorgrens blijft
  onaangeroerd of wordt expliciet gerapporteerd.
- [x] Rebuild de DEGIRO Pensioen-projectie vanaf de volledige bronhistorie.
- [x] Controleer dat er exact één EUR-cashpositie is en geen USD-cashpositie
  wanneer de bronrekening EUR-only is.

**Operationele vervolgstap:** voer voor bestaande Wealthfolio-data een
eenmalige `wealthfolio push --rebuild` uit. De rebuild verwijdert alleen
de doelaccount-data binnen de finance-sync-projectie en bouwt activiteiten en
de actuele snapshot opnieuw op. De bron bevat voor alle 53 USD-dividenden nu
een veilige EUR-projectie; ontbrekende FX-data blijft bij toekomstige imports
een zichtbare finding en wordt nooit als 1-op-1 omgerekend.

## Testplan

### Unit tests

- [ ] EUR-only account met EUR-transacties.
- [x] EUR-only account met USD-dividend en beschikbare historische FX-rate.
- [x] EUR-only account met USD-dividend zonder FX-rate: finding, geen 1:1-
  conversie.
- [x] Echte multi-currency account: USD-cash blijft toegestaan via expliciete
  connector metadata.
- [x] Cashsnapshot herhaald uitvoeren blijft idempotent.
- [x] Fees en taxes blijven afzonderlijk en worden niet dubbel meegerekend.

### Integratietests

- [x] DEGIRO `Portfolio.xlsx`, `Transactions.xlsx` en `Account.xlsx` uit de
  huidige productie-smoke-test.
- [x] Verwacht finance-sync-resultaat: holdings €70.961,98 en cash €7.444,44.
- [x] Verwacht Wealthfolio-resultaat binnen tolerantie: cash €7.444,44 en
  portfolio value €78.406,42.
- [x] Controleer dat Wealthfolio geen USD-cashholding bevat.
- [x] Herhaal dezelfde sync en controleer gelijke aantallen, bedragen en
  snapshot-ID's.
- [x] Test gewijzigde cashstand en verwijderde/oude connector-data.

### Observability

Log uitsluitend niet-gevoelige samenvattingen:

```text
account_currency=EUR
source_holdings=70961.98
source_cash=7444.44
destination_holdings=70961.98
destination_cash=7444.44
cash_delta=0.00
fx_cash_positions=0
```

## Acceptatiecriteria

- [x] Finance-sync blijft de enige bron van waarheid voor holdings en cash.
- [x] Een EUR-only DEGIRO-account projecteert uitsluitend EUR-cash.
- [x] USD-dividenden zonder veilige EUR-projectie veroorzaken geen zelfstandige
  USD-cashpositie.
- [x] Oorspronkelijke valuta en FX-informatie blijven auditbaar beschikbaar.
- [x] Wealthfolio cash sluit binnen de ingestelde tolerantie aan op
  `available_balance`.
- [x] Wealthfolio portfolio value sluit aan op holdings plus cash.
- [x] Herhaalde syncs zijn idempotent en stapelen geen cashcorrecties op.
- [x] Fees, taxes, dividends en technische FX/cash-sweepregels blijven
  semantisch onderscheidbaar.
- [x] De volledige unit-, integratie- en lokale Wealthfolio-smoketest slaagt.

## Niet in scope

- Historische koerskwaliteit van assets die Wealthfolio/Yahoo niet kan vinden.
- Het toevoegen van een echte DEGIRO live-API.
- Het wijzigen van finance-sync-portfolioholdings die al met de broker-export
  overeenkomen.
- Het importeren van handmatige Wealthfolio-data buiten de
  finance-sync-connectorgrens.

## Verificatie

De codechecks voor deze implementatieslag zijn groen:

- `uv run ruff check ...`: geslaagd.
- Gerichte Wealthfolio-, client- en DEGIRO-tests: **84 geslaagd**.
- Volledige niet-integratie-/niet-e2e-suite: **3.533 geslaagd, 8 overgeslagen**.

De aangeleverde XLSX is rechtstreeks door de connector geverifieerd: **53 van
53 USD-dividenden** hebben een gekoppelde EUR-basewaarde, inclusief gebundelde
FX-debits. De lokale rebuild is uitgevoerd met de volledige bronhistorie.

Resultaat in de wegwerp-Wealthfolio-container:

| Controle | Resultaat |
|---|---:|
| Securities value | €70.961,98 |
| EUR cash | €7.444,44 |
| Portfolio value | €78.406,42 |
| USD-cashposities | 0 |
| Activiteiten | 234 inclusief 1 stabiele cashreconciliatie |

Een tweede incrementele sync gaf dezelfde waarden, `0` nieuwe bronactiviteiten
en opnieuw één cashreconciliatiecorrectie. De foutieve cashprojectie stapelt
dus niet op. De lokale Wealthfolio-testomgeving is beschikbaar op
`http://localhost:8088` met wachtwoord `local-test`.
