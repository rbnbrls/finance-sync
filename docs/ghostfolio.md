# Ghostfolio-koppeling

## Wat de importer nodig heeft

Ghostfolio ondersteunt CSV en JSON. De browser-importer normaliseert deze CSV-kolommen:

`Date`, `Code`/`Symbol`, `DataSource`, `Currency`, `Price`, `Quantity`, `Action`, `Fee`, optioneel `Account` en `Note`.

Voor een directe koppeling gebruikt finance-sync de gedocumenteerde JSON API:

`POST /api/v1/import` met een bearer token en `{ "activities": [...] }`.

Elke activity bevat `currency`, `dataSource`, ISO-8601 `date`, numerieke `fee`, `quantity`, `symbol`, `type` en `unitPrice`; `accountId` en `comment` zijn optioneel. Ondersteunde activity types zijn `BUY`, `SELL`, `DIVIDEND`, `FEE`, `INTEREST` en `LIABILITY`. finance-sync gebruikt `YAHOO` voor opgeloste securities en `MANUAL` voor cash/ongeïdentificeerde instrumenten.

## Configuratie

Gebruik een extern beheerde Ghostfolio-installatie. Maak onder `My Ghostfolio -> Access` een security token aan en zet daarna de bearer token in `.env`:

```dotenv
GHOSTFOLIO_SERVER_URL=http://localhost:3333
GHOSTFOLIO_ACCESS_TOKEN=...
```

## Push

De Python-integratie is bewust idempotent op retry-niveau: activiteiten worden één voor één verstuurd. Ghostfolio meldt dubbele activiteiten als `400`; die worden als `skipped` geregistreerd, terwijl andere fouten de run laten falen. Een volledige run gebruikt alleen `booked` transactions en schrijft een `ExportRun` met `exporter_type=ghostfolio`.

```python
from datetime import UTC, datetime, timedelta

from finance_sync.exporter.ghostfolio.client import GhostfolioClient
from finance_sync.exporter.ghostfolio.config import GhostfolioConfig
from finance_sync.exporter.ghostfolio.exporter import GhostfolioExporter

config = GhostfolioConfig(server_url="http://localhost:3333", access_token="...")
async with GhostfolioClient(config) as client:
    await client.health()
    result = await GhostfolioExporter(session_factory, config, tenant_id).run_export(
        client, since=datetime.now(UTC) - timedelta(days=90)
    )
```

De Ghostfolio-database en Redis worden door de externe installatie beheerd.
