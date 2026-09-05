# Verbeterplan Wealthfolio-exporter

Status: geïmplementeerd en geverifieerd. Alle fasen zijn in de exporter
doorgevoerd: transactionele accounts, volledige backfill, asset- en
activiteitspayloads, historische quotes/FX, dataset-cleanup, rebuild en
regressietests.

## Doel

Wealthfolio moet als projectie van finance-sync volledig kunnen werken: historie,
holdings, kostprijs, gerealiseerde en ongerealiseerde winst, cashflow,
performance, inkomsten en multi-currency waardering moeten uitsluitend op de
finance-sync-dataset kunnen worden gebaseerd.

## Wat Wealthfolio nodig heeft

1. **Accountmodel**
   - Een `SECURITIES`-account met de juiste valuta en een stabiele
     `providerAccountId`.
   - `TRANSACTIONS`-tracking voor transaction-level historie, lots, kostbasis en
     performance. `HOLDINGS` is bedoeld voor handmatig/imported snapshots en
     levert niet dezelfde transactiehistorie.
   - De actuele finance-sync-accountselectie moet exact overeenkomen met de
     accounts die in Wealthfolio blijven bestaan.

2. **Volledige activiteitenhistorie**
   - Alle `POSTED` activiteiten vanaf de eerste beschikbare brontransactie.
   - Correcte datum van de gebeurtenis, activiteitstype, asset, quantity,
     unit price, bedrag, valuta, fee en tax.
   - `DEPOSIT`/`WITHDRAWAL` voor externe cashflows en expliciete transfer-
     semantiek voor interne of externe transfers.
   - Idempotente identifiers zodat een backfill geen duplicaten maakt.

3. **Assets en instrumentidentiteit**
   - Een stabiele Wealthfolio-assetkoppeling per finance-sync-security, bij
     voorkeur met ISIN plus ticker, instrumenttype en quote currency.
   - Dezelfde identiteit moet worden gebruikt door activiteiten, holdings en
     quotes; alleen een tickernaam is onvoldoende voor ambiguë of beurs-
     gekwalificeerde instrumenten.

4. **Lots en kostbasis**
   - BUY/SELL-activiteiten met quantity, unit price en expliciete valuta.
   - Fees moeten volgens Wealthfolio-semantiek in de trade worden opgenomen,
     zodat FIFO-lots en book cost worden berekend.
   - Asset transfers moeten quantity én kostbasis kunnen overdragen; splits,
     corporate actions en correcties mogen niet als een generieke fee eindigen.

5. **Waardering en historische grafieken**
   - Historische daily quotes vanaf de eerste activiteit tot vandaag voor elk
     instrument.
   - FX-rates voor vreemde valuta en een actuele quote per asset.
   - Cashbalansen en externe cashflows op de juiste datum.
   - Na een grote import/backfill moet Wealthfolio zijn historische waardering
     opnieuw kunnen berekenen.

Wealthfolio beschrijft deze afhankelijkheden expliciet: BUY/SELL creëren en
verbruiken lots en cost basis, terwijl performance op gedateerde activiteiten,
waarderingen en quotes steunt. Zie de [activity types reference](https://github.com/wealthfolio/wealthfolio/blob/main/docs/activities/activity-types.md),
het [tracking-mode model](https://github.com/wealthfolio/wealthfolio/blob/main/crates/core/src/accounts/accounts_model.rs)
en de [performance API](https://github.com/wealthfolio/wealthfolio/blob/main/apps/server/src/api/performance.rs).

## Geconstateerde gaps

### Kritiek

| Gap | Bewijs in finance-sync | Effect |
|---|---|---|
| Verkeerd account-trackingmodel | Opgelost: `create_account()` en `ensure_account()` gebruiken/migreren naar `TRANSACTIONS`. | Transaction history, lots, book cost en performance worden uit activiteiten opgebouwd. |
| Geen volledige historische backfill | Opgelost: first-sync gebruikt de oudste canonical transactie; `--full-history` forceert dit opnieuw. | Oudere BUY’s en openingslots worden meegenomen. |
| Geen historische quotes naar Wealthfolio | Opgelost: `SecurityPrice` en `FxRate` worden via de actuele quote- en exchange-rate API geprojecteerd. | Dashboard- en performancehistorie kunnen op brondata worden gewaardeerd. |

De drie blockers zijn opgelost: accountmigratie naar `TRANSACTIONS`,
tenant-/target-scoped historische startdatum en een idempotente projectie van
daily `SecurityPrice`-quotes met bron `FINANCE_SYNC`.

### Hoog

| Gap | Bewijs | Effect |
|---|---|---|
| Holdingssnapshot is alleen actueel | `_holdings_snapshot_payload()` zet bewust `date` op vandaag; snapshots worden in de normale `reconcile`-strategie niet opnieuw opgeslagen zodra Wealthfolio al posities heeft. | Dit maakt geen historische reeks. De historische reeks moet uit complete transacties plus quotes komen; de huidige fallback vult dat niet aan. |
| Asset-identiteit is niet volledig expliciet | `_wf_row_to_api_activity()` stuurt meestal een ticker; alleen wanneer de ticker zelf ISIN-vorm heeft wordt `isin` toegevoegd. De mapper kiest ticker vóór ISIN, terwijl de documentatie anders beweert. | Wealthfolio kan activiteiten zonder bruikbaar asset opslaan; dat past bij de bestaande live-fixture met lege `assetId`/`assetSymbol` en veroorzaakt nul holdings/kostbasis. |
| Broncorrecties worden niet gespiegeld | Opgelost voor stale records: iedere sync verwijdert activiteiten zonder actuele finance-sync-ID; `--rebuild` verwijdert en importeert opnieuw. | Provider-revisies en ingetrokken transacties verdwijnen uit de projection. |

### Middel

| Gap | Effect |
|---|---|
| `TRANSFER_IN/OUT`, splits, corporate actions en subtype/metadata zijn niet volledig gemodelleerd in de exporter. | Interne transfers kunnen als externe cashflow worden geïnterpreteerd; kostbasis en TWR kunnen afwijken. |
| Fee/tax-contract is onvoldoende end-to-end geverifieerd. | De mapper heeft `fee`, maar er ontbreekt een live-contracttest die bevestigt dat Wealthfolio die fee werkelijk in FIFO/book cost verwerkt. |
| `_last_export_time()` is niet tenant-scoped en is een globale fallback voor legacy exports. | Een andere tenant/export kan de historische startdatum beïnvloeden. |
| Er is geen gecontroleerde “rebuild destination” voor een bestaande target. | Opgelost: `--rebuild` verwijdert accountactiviteiten, waarna volledige historie opnieuw wordt geïmporteerd. |

De live-contractfixture ondersteunt de diagnose: de eerder geregistreerde
activiteiten bevatten correcte datums, maar de smoke-activiteiten hebben lege
`assetId`/`assetSymbol` en `fee=0.0`; dat is geen bewijs dat echte providerdata
altijd fout is, maar wel dat de huidige test geen volledige asset- en kostbasis-
keten valideert.

## Verbeterplan

### Fase 1 — Wealthfolio-accountcontract corrigeren

- Maak alle broker-/investmentaccounts aan met `TRANSACTIONS`.
- Update bestaande finance-sync-accounts expliciet naar `TRANSACTIONS` en behoud
  bestaande naam, provideridentiteit, valuta en accountinginstellingen.
- Laat cashaccounts eveneens transactioneel blijven.
- Voeg een contracttest toe die controleert dat een bestaande `HOLDINGS`-
  account wordt gemigreerd en daarna activiteiten verwerkt.

### Fase 2 — Full-sync en veilige cursorstrategie

- Voeg een expliciete first-sync/backfillmodus toe die de oudste beschikbare
  finance-sync-transactie als start neemt, niet standaard 90 dagen.
- Bewaar per destination/account een datasetversie of sync generation.
- Maak full-resync idempotent: bestaande finance-sync-activiteiten worden
  herkend via een stabiele idempotency key en niet gedupliceerd.
- Scheid “incremental sync” en “rebuild destination” zichtbaar in de exporter-
  API/UI.
- Fix de tenant-scoping van de legacy export-timestamp.

**Status:** geïmplementeerd. Gebruik `--full-history` voor een historische
backfill en `--rebuild` voor het opnieuw opbouwen van activiteiten.

### Fase 3 — Asset- en activiteitspayload compleet maken

- Stuur een deterministische asset identity mee: ISIN, ticker, instrumenttype,
  quote currency en waar mogelijk exchange/MIC.
- Maak één mappinglaag voor dezelfde assetidentiteit in activiteiten, holdings
  en quotes.
- Map alle ondersteunde canonical transaction types expliciet: BUY, SELL,
  DIVIDEND, INTEREST, DEPOSIT, WITHDRAWAL, FEE, TAX, TRANSFER_IN,
  TRANSFER_OUT, SPLIT, CREDIT en ADJUSTMENT.
- Voeg `metadata.flow.is_external`, subtype, bron-ID en providerbron toe waar
  Wealthfolio die semantiek gebruikt.
- Leg vast hoe provider-revisies en reversals als Wealthfolio update/void
  worden verwerkt.

**Status:** geïmplementeerd. De API-payload bevat ISIN naast ticker,
instrumenttype, metadata en transfer-flow; `SPLIT`, `CREDIT` en `ADJUSTMENT`
worden expliciet gemapt.

### Fase 4 — Quotes, FX en actuele waarde

- Exporteer finance-sync `SecurityPrice` daily history naar de Wealthfolio
  quote/market-data API, inclusief currency, source en timestamp.
- Exporteer historische FX-rates of documenteer en test Wealthfolio’s eigen FX-
  bron als canonical fallback.
- Gebruik de holdingssnapshot alleen als actuele controle/bootstrap; gebruik
  hem niet als vervanging voor transacties of historische waardering.
- Voeg na backfill een expliciete recalculation/health-check toe en controleer
  historische coverage per asset.

**Status:** geïmplementeerd. Daily `SecurityPrice`- en `FxRate`-historie wordt
naar Wealthfolio quotes geprojecteerd met actuele Wealthfolio-API-velden en
connectorbron.

### Fase 5 — Datasetreconciliatie en cleanup

- Vergelijk vóór en na iedere sync de finance-sync dataset met Wealthfolio:
  accounts, activiteiten, assets, lots, current holdings en quotes.
- Verwijder vreemde accounts/data binnen de destination ownership boundary;
  verwijder of void ook oude finance-sync-activiteiten die niet meer in de
  actuele bronset zitten.
- Rapporteer afzonderlijk: ontbrekende historie, unresolved assets, ontbrekende
  quotes, afwijkende quantity, book-cost-afwijking en cashflow-afwijking.
- Laat een sync falen wanneer volledige projection niet is bereikt, in plaats
  van alleen een holdingsreconciliatie-waarschuwing te produceren.

**Status:** geïmplementeerd. Accounts en stale activiteiten worden bij iedere
sync opgeschoond; `--rebuild` verwijdert alle geselecteerde activiteiten en
bouwt de projection opnieuw op.

### Fase 6 — Verificatie en rollout

- Bouw een deterministische fixture met meerdere jaren BUY/SELL, fees, tax,
  dividend, deposit, withdrawal, transfer, split, multi-currency en een
  gesloten positie.
- Controleer na import minimaal:
  - activity dates blijven historisch;
  - account tracking mode is `TRANSACTIONS`;
  - holdings quantity klopt per datum;
  - book cost/FIFO en realized gain zijn niet nul;
  - dashboard/net-worth/performance bevat meerdere historische punten;
  - fees en taxes verschijnen in Insights;
  - herhaalde sync is idempotent;
  - full-resync verwijdert/voidt stale finance-sync-data zonder vreemde data
    te laten staan.
- Voer eerst een dry-run uit op productie, daarna een backup en een eenmalige
  rebuild van de Wealthfolio-destination. Behoud finance-sync als canonical
  bron en bewaar export-run-auditinformatie.

**Status:** geïmplementeerd en lokaal geverifieerd. De productieactie blijft
operationeel: maak eerst een Wealthfolio-backup en voer daarna
`finance-sync wealthfolio push --full-history --rebuild` uit.

## Aanbevolen volgorde

Fase 1 en 2 zijn blockers voor betrouwbare historie en book cost. Fase 3 en 4
leveren de ontbrekende asset- en waarderingsdata. Fase 5 en 6 maken het gedrag
operationeel veilig en aantoonbaar compleet. Pas na fase 6 is het verantwoord
om de bestaande productie-Wealthfolio-dataset opnieuw op te bouwen.
