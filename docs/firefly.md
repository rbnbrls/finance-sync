# Firefly III-koppeling

De lokale Firefly-teststack is verwijderd. Gebruik een extern beheerde Firefly
III-installatie en zet het personal access token in de finance-sync omgeving:

```bash
export FIREFLY_SERVER_URL=http://localhost:8082
export FIREFLY_ACCESS_TOKEN='...'
```

De koppeling is vervolgens beschikbaar via:

```text
POST /api/v1/exporters/firefly/export
GET  /api/v1/exporters/firefly/config
```

De request maakt ontbrekende asset accounts aan en schrijft transacties via
`POST /api/v1/transactions`. Elke split bevat `external_id` (de provider
transaction ID), een finance-sync note en de tag `finance-sync`. Firefly's
duplicate-hash guard staat aan; een retry na een timeout is daardoor veilig.

## Welke data heeft de Data Importer nodig?

Voor CSV-import zijn twee bestanden nodig: de CSV zelf en een JSON-configuratie
die eenmalig in de webinterface wordt aangemaakt/geëxporteerd. De JSON bevat
onder meer delimiter, header-instelling, datumformaat, duplicate-detection,
import-account, import-tag en de rol per kolom.

De minimale bruikbare kolommen zijn:

| Kolom | Importer-rol | Herkomst finance-sync |
|---|---|---|
| `date` | transaction date | `occurred_at` |
| `amount` | amount | absolute waarde van `amount` |
| `description` | description | `description` / `transaction_type` |
| `source` | source account | asset account bij withdrawal |
| `destination` | destination account | expense/revenue account |
| `external_id` | external ID | `external_transaction_id` |
| `currency` | currency | `currency_code` |
| `notes` | notes | canonical transaction UUID |

`source` en `destination` zijn richtingafhankelijk: een negatieve canonical
amount wordt een withdrawal van het asset account; een positieve amount wordt
een deposit naar het asset account. Bij CSV-import moet je in de importer de
kolomrollen opnieuw valideren en het standaard import-account kiezen als een
accountnaam ontbreekt.

De importer voert eerst conversie/validatie uit en daarna de daadwerkelijke
import. Voor CLI-import moet de config naast de CSV staan en moet de map als
`IMPORT_DIR_ALLOWLIST` zijn toegestaan:

```bash
docker run --rm \
  -v "$PWD/var/firefly-import:/import" \
  -e FIREFLY_III_ACCESS_TOKEN="$FIREFLY_ACCESS_TOKEN" \
  -e FIREFLY_III_URL=http://host.docker.internal:8082 \
  -e IMPORT_DIR_ALLOWLIST=/import \
  -e WEB_SERVER=false \
  fireflyiii/data-importer:latest-cli
```

In deze repository is de geautomatiseerde koppeling API-gebaseerd; de
importer blijft beschikbaar voor bank-CSV/CAMT-bestanden en handmatige
validatie. De API-route voorkomt dat finance-sync bij iedere run afhankelijk
is van een door de importer UI geëxporteerde, bank-specifieke JSON-config.

Bronnen: [CSV/CAMT import](https://docs.firefly-iii.org/how-to/data-importer/import/csv/),
[CLI import](https://docs.firefly-iii.org/how-to/data-importer/advanced/cli/),
[Firefly API](https://www.mintlify.com/firefly-iii/firefly-iii/api/overview).
