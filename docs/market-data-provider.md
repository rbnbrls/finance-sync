# Finance-sync als Wealthfolio Market Data Provider

Finance-sync exposes a downstream market-data API at `/api/v1/market-data`.
The first live adapter is Trading212. The API key must have the
`market-data:read` permission and is sent as `X-API-Key`.

## Wealthfolio configuration

Add a Custom Provider under **Settings → Market Data**:

| Field | Value |
| --- | --- |
| Latest URL | `http://localhost:8000/api/v1/market-data/latest?symbol={SYMBOL}&connection_id=<CONNECTION_ID>` |
| Latest price path | `$.price` |
| Latest date path | `$.timestamp` |
| Historical URL | `http://localhost:8000/api/v1/market-data/history?symbol={SYMBOL}&from={FROM}&to={TO}` |
| Historical price path | `$.data[*].price` |
| Historical date path | `$.data[*].date` |
| Currency path | `$.currency` (latest) |

Configure the API key as an authentication header:

```text
X-API-Key: __SECRET__<FINANCE_SYNC_API_KEY>
```

When Wealthfolio itself runs in Docker, use
`http://host.docker.internal:8000` for finance-sync on macOS/Windows. For a
shared Docker network, use the finance-sync service name instead.

`connection_id` is optional. It is recommended when multiple Trading212
connections exist. Historical requests read the local `security_prices`
cache. Trading212's portfolio endpoint supplies current prices, not historical
candles, so finance-sync does not fabricate missing historical data.

