# Actual Budget-koppeling

## Lokale testserver

De lokale testserver gebruikt de officiële `actualbudget/actual-server`-image en
luistert op <http://localhost:5006>. Budgetbestanden worden persistent bewaard
in `var/actual-data`.

```sh
docker compose -f docker-compose.actual.yml up -d
```

Open daarna <http://localhost:5006>, stel het lokale serverwachtwoord in en
maak een budget aan. Voor de huidige lokale testomgeving is het wachtwoord
`finance-sync-local` en de budgetnaam `Finance Sync Local` aangemaakt. Gebruik
voor echte omgevingen een ander wachtwoord.

Stoppen zonder data te verwijderen:

```sh
docker compose -f docker-compose.actual.yml stop
```

## Welke importer-data Actual nodig heeft

Actual kan CSV, QIF, OFX, QFX en CAMT importeren. Voor CSV is geen vast
bankformaat vereist: de gebruiker koppelt kolommen in de importwizard aan de
velden van Actual. De relevante velden zijn:

| Veld | Nodig | finance-sync-bron |
| --- | --- | --- |
| Datum | ja | `occurred_at` als `YYYY-MM-DD` |
| Bedrag | ja | `amount`, signed; positieve bedragen zijn inkomsten |
| Payee | aanbevolen | `description` |
| Notes | optioneel | FX-gegevens, type en provider reference |
| Category | optioneel | leeg gelaten; Actual-regels kunnen categoriseren |
| Imported ID | API/import, niet CSV-wizard | `fs_<external_transaction_id>` |
| Imported payee | API/import, niet CSV-wizard | oorspronkelijke `description` |
| Cleared | API/import | `status == booked` |

De API gebruikt integer cents. De koppeling gebruikt daarom `Decimal * 100` en
behoudt het teken. Voor betrouwbare deduplicatie wordt `imported_id` gebruikt;
Actual matcht daarnaast op datum, bedrag en vergelijkbare payee.

Bronnen: [CSV/file import](https://actualbudget.org/docs/transactions/importing/),
[API transaction model en importTransactions](https://actualbudget.org/docs/api/reference/),
en [officiële Docker-installatie](https://actualbudget.org/docs/install/docker/).

## finance-sync ontwerp

`ActualBudgetExporter` leest genormaliseerde `Account`- en `Transaction`-rijen,
maakt de corresponderende Actual-account aan, en importeert transacties met
Actuals reconcile-flow. `ActualBudgetAccountMapping` bewaart de koppeling van
account-ID naar Actual-account-ID/naam. `ExportDelivery` bewaart per tenant,
bestemming en account de cursor; een retry kan daardoor geen andere bestemming
overschrijven. De `imported_id` maakt de externe kant eveneens idempotent.

CLI:

```sh
finance-sync actual-budget export --help
finance-sync actual-budget push --help
```

Minimale instellingen:

```dotenv
EXPORTER_ACTUAL_BUDGET_ENABLED=true
ACTUAL_BUDGET_SERVER_URL=http://localhost:5006
ACTUAL_BUDGET_PASSWORD=...
ACTUAL_BUDGET_BUDGET_NAME=Finance Sync Local
```

`export` voert de normale cyclus uit en schrijft tevens een CSV-samenvatting;
`push` is de expliciete directe serveractie.

