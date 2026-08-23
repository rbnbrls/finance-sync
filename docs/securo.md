# Securo-koppeling

## Lokale testinstallatie

```sh
./scripts/setup-securo-test.sh
```

De compose-stack gebruikt poort `3001` voor de frontend en `8001` voor de backend, zodat de standaard finance-sync-stack niet wordt overschreven. De Securo-code wordt onder `var/securo-test/` geplaatst en de database in een named volume.

Open daarna <http://localhost:3001>, maak een gebruiker aan en gebruik diens e-mailadres en wachtwoord voor de push.

## Datacontract van de importer

Securo accepteert OFX, QIF, CAMT en CSV. De koppeling gebruikt CSV met deze kolommen:

| Kolom | Verplicht | Betekenis |
|---|---:|---|
| `date` | ja | ISO-datum `YYYY-MM-DD` |
| `description` | ja | omschrijving/payee |
| `amount` | ja | positief bedrag; `type` bepaalt richting |
| `type` | nee | `credit` of `debit` |
| `currency` | nee | ISO-4217 |
| `external_id` | nee, aanbevolen | stabiele finance-sync-ID voor deduplicatie |
| `payee` | nee | ruwe payee |
| `notes` | nee | provenance/notitie |

De importer doet eerst preview en daarna confirm. De API-endpoints zijn `POST /api/transactions/import/preview` en `POST /api/transactions/import`; beide vereisen een bearer-sessie-token. De push gebruikt expliciete column mapping, duplicate detection en maakt ontbrekende Securo-accounts automatisch aan.

## Gebruik

```sh
finance-sync securo export --days-back 90
finance-sync securo push --server-url http://localhost:3001 \
  --email you@example.com --password 'local-password'
```

Per finance-sync-account wordt een CSV-bestand geschreven. De push importeert elk bestand in het gelijknamige Securo-account en rapporteert imported/skipped aantallen.
