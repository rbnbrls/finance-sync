# DEGIRO Pensioen naar Wealthfolio

finance-sync koppelt officiële, handmatig gedownloade DEGIRO-exports aan een
self-hosted Wealthfolio-installatie. Dit is geen live DEGIRO-API: finance-sync
logt niet bij DEGIRO in en bewaart geen gebruikersnaam, wachtwoord of 2FA-code.

## Eerste import

1. Configureer de `degiro_pension`-connector en importeer transactieoverzicht,
   rekeningoverzicht en portefeuillesnapshot via de beheer-UI.
2. Controleer de preview op ontbrekende rapporten en unresolved securities.
3. Bevestig de import.
4. Configureer `WEALTHFOLIO_SERVER_URL` en `WEALTHFOLIO_PASSWORD` en voer
   `finance-sync wealthfolio push --account-ids <finance-sync-account-id>` uit.

Wealthfolio krijgt één `SECURITIES`-account met een stabiele
`providerAccountId` per finance-sync-account. Daardoor blijft `DEGIRO Pensioen`
gescheiden van gewone DEGIRO-, bunq- en Trading212-rekeningen, ook als namen
overeenkomen. Bestaande koppelingen staan in
`wealthfolio_account_mappings`.

## Activiteiten en valuta

De historie is leidend (`activity-first`). Aankopen, verkopen, stortingen,
opnames, dividend, rente, kosten en dividendbelasting worden respectievelijk
als `BUY`, `SELL`, `DEPOSIT`, `WITHDRAWAL`, `DIVIDEND`, `INTEREST`, `FEE` en
`TAX` verzonden. Dividendbelasting blijft dus een afzonderlijke activiteit.

Trades behouden quantity, unit price, instrumentvaluta, transactiekosten en de
DEGIRO-FX-rate. Het EUR-basisbedrag blijft in finance-sync beschikbaar voor
controle. De ticker wordt gebruikt voor marktdata en ISIN wordt daarnaast
meegenomen wanneer de bron geen ticker heeft. Een activiteit of holding zonder
opgeloste security stopt bij de exportcursor en verschijnt als mislukte
export/review-item; finance-sync kiest nooit stil een gelijkende ticker.

## Holdings en reconciliatie

`WEALTHFOLIO_HOLDINGS_STRATEGY=reconcile` is de veilige standaard. Wealthfolio
bouwt posities uit activiteiten op; de nieuwste DEGIRO-snapshot wordt alleen
gebruikt om quantities en totale waarde te controleren. Zo wordt de
portefeuillewaarde niet dubbel opgenomen.

Voor een leeg Wealthfolio-account kan
`WEALTHFOLIO_HOLDINGS_STRATEGY=bootstrap` worden gebruikt. finance-sync voert
eerst Wealthfolio's holdings-preview uit en importeert de snapshot uitsluitend
als het remote account nog geen holdings heeft. Daarna blijft de strategie
activity-first.

De toegestane waardeafwijking is het grootste van:

- `WEALTHFOLIO_RECONCILIATION_ABSOLUTE_TOLERANCE` (standaard `1.00`);
- `WEALTHFOLIO_RECONCILIATION_PERCENTAGE_TOLERANCE` (standaard `0.005`, dus
  0,5%).

Een quantity- of waardeafwijking markeert de export als mislukt en blijft via
de exporter-run API zichtbaar. De geslaagde DEGIRO-bronimport wordt daardoor
niet teruggedraaid.

## Periodiek bijwerken en herstel

De worker hervat iedere vijf minuten vanaf de per-account
`wealthfolio_deliveries`-cursor. Identieke en overlappende DEGIRO-exports maken
geen nieuwe canonical transacties; een geslaagde Wealthfolio-push schuift de
cursor pas op nadat het hele accountbatch is geaccepteerd. Bij een storing
blijft de cursor staan en kan dezelfde export veilig opnieuw worden geprobeerd.

Los unresolved securities eerst op via de security-reviewflow en probeer de
mislukte exporter-run daarna opnieuw. Controleer bij freshnessproblemen de
laatste DEGIRO `ImportRun`, daarna de laatste Wealthfolio `ExportRun`.

Voor een eerste volledige projectie gebruik je
`finance-sync wealthfolio push --full-history`. Iedere push verwijdert remote
accounts buiten de exacte finance-sync-dataset; een volledige backfill is
idempotent via de Wealthfolio-import en de per-account cursor. Bewaar oude
import- en export-runs als auditspoor. Broncorrecties op reeds geïmporteerde
activiteiten worden nog niet automatisch ge-void of vervangen; dat blijft een
open punt in het verbeterplan.

## Productie-smoke-test

Voer na configuratie uit:

```bash
finance-sync wealthfolio smoke --account-ids <finance-sync-account-id>
```

De smoke-run pusht tweemaal, controleert accountzichtbaarheid, aantallen,
holdings/reconciliatie en eist dat de tweede passage niets nieuws importeert.
De uitvoer bevat alleen aantallen en gezondheidsstatus: geen financiële
waarden, wachtwoord of andere credentials. Gebruik hiervoor bij voorkeur een
afzonderlijke operatoromgeving en publiceer de uitvoer niet als CI-artifact.
