# Statische stagingdataset — juli 2026

Deze volledig synthetische dataset bevat één kalendermaand (`2026-07-01` tot
en met `2026-07-31`) en mag veilig in staging en CI worden gebruikt. IDs,
rekeningnummers, namen, koersen en bedragen zijn testdata; er staan geen echte
rekeninggegevens of credentials in.

## Inhoud

| Provider | Data | Verwacht |
|---|---|---:|
| bunq | Eén betaalrekening en iedere dag precies één betaling | 31 transacties |
| DEGIRO Pensioen | Vier officiële-export-achtige trades plus dividend op de vijf vrijdagmomenten | 5 beleggingsgebeurtenissen |
| Trading212 | Vier orders plus dividend op de vijf vrijdagmomenten | 5 beleggingsgebeurtenissen |

Beide brokerdatasets bevatten aandelen (`AAPL`, `ASML`), ETF's (`VWCE`,
`IWDA`) en een afzonderlijke dividendbetaling. DEGIRO bevat daarnaast een
aparte dividendbelastingregel. Beide brokers hebben een portefeuillesnapshot
met vier posities.

## Bestanden en laden

- `bunq/*.json` volgt de response-enveloppen die de ingebouwde bunq-connector
  verwacht. Koppel de routes uit `manifest.json` aan een staging mockserver en
  zet `base_url` van de connector naar die server.
- `trading212/*.json` volgt de Trading212 v0 account-, history- en
  portfolioresponses. Gebruik ook hier de routetabel uit `manifest.json`.
- `degiro/*.csv` representeert de officiële Nederlandse transactie-, rekening-
  en portefeuille-export. De bestanden hebben een UTF-8 BOM, komma als
  scheidingsteken en de herhaalde lege valutaheaders die in DEGIRO-exports
  voorkomen. Deze kunnen worden geladen zodra `degiro_pension` beschikbaar is.

`manifest.json` bevat de periode, verwachte aantallen, dagelijkse dekking en
mockserverroutes. De JSON-history-responses staan nieuwste-eerst waar de
provider dat doet.

## Reproduceren en valideren

```bash
uv run python scripts/generate_staging_dataset.py
uv run pytest tests/test_staging_dataset.py
```

De generator gebruikt vaste datums, IDs en waarden. Opnieuw genereren hoort
daarom geen diff te geven.
