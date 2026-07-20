# REST API specification

Base URL: `/api/v1`. JSON uses lower camel case externally, RFC 3339 timestamps, decimal values encoded as strings, and ISO currencies. Every collection endpoint supports `limit` (1–500), opaque `cursor`, `from`, `to`, and an `asOf` timestamp where meaningful. Responses include `meta: {asOf, currency, nextCursor, freshness}`.

Authentication is `Authorization: Bearer <JWT>` or `X-API-Key`. Mutations require `Idempotency-Key`; replay returns the original result. Errors use RFC 9457 Problem Details, with a correlation ID.

## Resources

| Method/path | Scope | Contract |
|---|---|---|
| `GET /accounts` | `finance:read` | Accounts, latest balances, institution and connection health. Filters: type, institutionId. |
| `GET /accounts/{id}` | `finance:read` | One account and current snapshot. |
| `GET /transactions` | `finance:read` | Canonical cash transactions. Filters: accountId, status, type, from, to, currency. |
| `GET /holdings` | `investments:read` | Latest or `asOf` holdings; filters portfolioId, securityId. |
| `GET /portfolio` | `investments:read` | Valuation, cash, gains, positions, freshness. |
| `GET /performance` | `analytics:read` | Time-series and return metrics. Parameters subject, period, from, to. |
| `GET /allocation` | `analytics:read` | Allocation by asset class, sector, country, currency, or security. |
| `GET /net-worth` | `analytics:read` | Cash + investments net-worth series and coverage. |
| `GET /cashflow` | `analytics:read` | Income/expense aggregates and transaction counts. |
| `GET /prices` | `market:read` | Latest/historical prices. Requires securityId/listingId; granularity and date range. |
| `GET /dividends` | `investments:read` | Dividend records and aggregate filters. |
| `POST /sync` | `sync:write` | Starts allowed connections; `{providers?, resources?, force?}`. Returns 202 sync-run links. |
| `POST /sync/{provider}` | `sync:write` | Starts one configured provider; provider is registry key, not a URL. |
| `GET /sync-runs/{id}` | `sync:read` | Status, cursors, counts, warnings, error code. |
| `GET /health` | public/internal | Liveness/readiness/dependency checks; redact details publicly. |
| `GET /metrics` | internal | Prometheus exposition, network-restricted. |

## AI resources

AI routes require `ai:read`, accept `currency` and `asOf`, and intentionally return bounded, source-cited summaries rather than raw paginated ledgers.

| Path | Response focus |
|---|---|
| `GET /ai/context` | Data coverage, accounts/portfolios, total values, freshness and caveats. |
| `GET /ai/networth` | Current and trailing series, component deltas, valuation coverage. |
| `GET /ai/portfolio` | Holdings, allocation, gains, concentration and stale prices. |
| `GET /ai/monthly-summary` | Income, expenses, cash-flow, notable changes for requested month. |
| `GET /ai/dividends` | Paid/expected dividend summary and recent events. |
| `GET /ai/subscriptions` | Recurring-payment candidates with confidence and evidence transaction IDs. |

Example response shape:

```json
{"data":{"asOf":"2026-07-20T10:00:00Z","currency":"EUR","netWorth":"125000.00","coverage":{"accounts":4,"pricedHoldings":18,"staleHoldings":1},"caveats":["One US listing price is 18 minutes old"]},"meta":{"correlationId":"...","freshness":"partial"}}
```

Version only breaking changes in `/api/v2`; add optional fields and endpoints without a major version. Publish OpenAPI at `/openapi.json`, Swagger at `/docs`, and a generated client only after API contract tests are established.
