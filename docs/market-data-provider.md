# Finance-sync als Wealthfolio Market Data Provider

Finance-sync exposes a downstream market-data API at `/api/v1/market-data`.
The generic endpoint serves the latest finance-sync holding/price data and
uses the local price cache for history. When Wealthfolio sends an ISIN-only
symbol that is not present in the finance-sync tenant, the endpoint resolves
the identifier through Yahoo's public search/chart endpoints and returns only
real observations (never a synthetic price). Historical responses merge local
cache rows with the resolved external series, which fills gaps in older
valuation dates. Trading212 live data remains available as a compatibility
fallback when `connection_id` is supplied. The API key must have the
`market-data:read` permission and is sent as `X-API-Key`.

## Wealthfolio configuration

Add a Custom Provider under **Settings → Market Data**:

| Field | Value |
| --- | --- |
| Latest URL | `http://localhost:8000/api/v1/market-data/latest?symbol={SYMBOL}` |
| Latest price path | `$.price` |
| Latest date path | `$.date` |
| Historical URL | `http://localhost:8000/api/v1/market-data/history?symbol={SYMBOL}&from={FROM}&to={TO}` |
| Historical price path | `$.data[*].price` |
| Historical date path | `$.data[*].date` |
| Currency path | `$.currency` (latest) |

Set the latest source's currency path to `$.currency`. This is important for
ISIN-only assets and prevents Wealthfolio from inferring a wrong quote
currency. Keep the historical source's price path as `$.data[*].price` and
date path as `$.data[*].date`; the response also exposes `$.currency` for
providers that support a historical currency mapping.

Configure the API key as an authentication header:

```text
X-API-Key: __SECRET__<FINANCE_SYNC_API_KEY>
```

When Wealthfolio itself runs in Docker, use
`http://host.docker.internal:8000` for finance-sync on macOS/Windows. For a
shared Docker network, use the finance-sync service name instead.

Use the **Wealthfolio market-data provider** button on the Exporters page to
create the least-privilege key and copy the exact URLs, JSON paths, and Docker
host for the current installation. `connection_id` is optional and only needed
for the legacy Trading212 live adapter. Historical requests read the local
`security_prices` cache; finance-sync does not fabricate missing historical
data.
