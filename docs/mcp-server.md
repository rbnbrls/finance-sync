# MCP Server — finance-sync Model Context Protocol Integration

This document describes the MCP (Model Context Protocol) server that
exposes finance-sync as a tool-accessible data source for AI assistants
(Hermes Agent, Claude Desktop, Cursor, and any MCP-compatible client).

## Overview

The MCP server wraps the finance-sync REST API and domain services into
an MCP-compatible interface using **SSE (Server-Sent Events)** transport.
This allows LLMs to query your financial data and trigger actions
directly through the MCP protocol.

## Architecture

```
┌─────────────────────┐       SSE/HTTP        ┌──────────────────────┐
│  AI Assistant       │ ◄──────────────────► │  MCP Server          │
│  (Hermes, Claude,   │                       │  (port 8100)         │
│   Cursor, etc.)     │                       │                      │
└─────────────────────┘                       │  ┌────────────────┐  │
                                              │  │ FastMCP        │  │
                                              │  │ + Auth Middle  │  │
                                              │  └───────┬────────┘  │
                                              │          │            │
                                              │  ┌───────▼────────┐  │
                                              │  │  Container     │  │
                                              │  │  (DB, Redis)   │  │
                                              │  └───────┬────────┘  │
                                              │          │            │
                                              │  ┌───────▼────────┐  │
                                              │  │ ReadService    │  │
                                              │  │ AISummary     │  │
                                              │  │ SyncOrch.     │  │
                                              │  └────────────────┘  │
                                              └──────────────────────┘
```

The MCP server runs as a **separate process** alongside the main REST API.
It shares the same database via the DI Container.

## Quick Start

### Prerequisites

- finance-sync installed with the `mcp` extras:
  ```bash
  uv sync --extra mcp
  ```
- A running PostgreSQL database (and optionally Redis)
- `.env` configured with at least `DATABASE_URL` and `SECRET_KEY`

### Running

#### As a standalone process (SSE transport, recommended)

```bash
uv run python -m finance_sync.mcp
```

This starts the MCP server on `http://0.0.0.0:8100` with the SSE endpoint
at `/sse` and the message POST endpoint at `/messages/`.

#### With uvicorn directly

```bash
uv run uvicorn finance_sync.mcp.server:app --host 0.0.0.0 --port 8100
```

#### With the MCP CLI (stdio transport, for testing)

```bash
mcp run src/finance_sync/mcp/server.py
```

### Configuration

| Variable    | Default     | Description                    |
|-------------|-------------|--------------------------------|
| `MCP_PORT`  | `8100`      | Port for the MCP SSE server.   |
| `MCP_HOST`  | `0.0.0.0`   | Host address for the MCP SSE server. |

Set these in your `.env` file or as environment variables.

## Authentication

The MCP server supports the same two authentication modes as the main
REST API:

1. **JWT Bearer token** — pass `Authorization: Bearer <token>` in
   the SSE connection request and all message POST requests.
2. **API key** — pass `X-API-Key: <key>` header.

For SSE connections you may also pass the token as a query parameter:

```
GET /sse?access_token=<jwt_token>
```

Authentication is enforced by a Starlette middleware that validates
credentials against the same JWT / API-key stores as the REST API.
Unauthenticated requests receive a **401 Unauthorized** response.

## Resources

Resources expose read-only financial data. Every resource returns
`application/json`.

### `finance://accounts`

Accounts list with current balances.

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "Checking Account",
      "account_type": "checking",
      "currency_code": "EUR",
      "current_balance": 1234.56,
      "available_balance": 1200.00,
      "is_active": true
    }
  ],
  "total": 5
}
```

### `finance://portfolio`

Current portfolio with per-account holdings breakdown.

```json
{
  "accounts": [
    {
      "account_id": "uuid",
      "account_name": "Investment Account",
      "holdings": [
        {
          "ticker": "AAPL",
          "security_name": "Apple Inc.",
          "quantity": 10,
          "market_value": 1500.00,
          "cost_basis": 1400.00,
          "unrealised_pl": 100.00,
          "unrealised_pl_pct": 7.14
        }
      ]
    }
  ],
  "total_value": 15000.00,
  "currency_code": "EUR"
}
```

### `finance://transactions`

Last 50 transactions across all accounts.

```json
[
  {
    "id": "uuid",
    "account_id": "uuid",
    "account_name": "Checking",
    "amount": -45.00,
    "description": "Coffee shop",
    "occurred_at": "2026-07-20T10:30:00Z",
    "transaction_type": "payment"
  }
]
```

### `finance://net-worth`

Current net worth (total assets minus total liabilities).

```json
{
  "total_assets": 100000.00,
  "total_liabilities": 25000.00,
  "net_worth": 75000.00,
  "currency_code": "EUR",
  "as_of": "2026-07-21T12:00:00Z"
}
```

## Tools

Tools are actions the AI assistant can invoke.

### `run_sync`

**Parameters:**

| Parameter        | Type   | Description                                        |
|------------------|--------|----------------------------------------------------|
| `connector_type` | string | Connector name, e.g. `"bunq"`, `"trading212"`.     |

Triggers a manual sync for the given connector type. Credentials are
fetched from the credential store (AES-256-GCM encrypted at rest).

**Returns:**

```json
{
  "status": "completed",
  "accounts_synced": 3,
  "transactions_synced": 47,
  "error_message": null,
  "duration_s": 2.34
}
```

### `get_summary`

**Parameters:**

| Parameter   | Type   | Description                                        |
|-------------|--------|----------------------------------------------------|
| `timeframe` | string | Time period, e.g. `"7d"`, `"30d"`, `"90d"`. Default: `"30d"` |

Generates an AI-powered natural-language summary of recent financial
activity. Requires the AI provider to be configured (`AI_ENABLED=true`,
`AI_API_KEY` set).

**Returns:**

```json
{
  "summary": "Over the past 30 days, total spending was €2,340 while income was €4,500...",
  "generated_at": "2026-07-21T12:00:00Z",
  "source": "ai_generated",
  "model": "gpt-4o"
}
```

### `resolve_security`

**Parameters:**

| Parameter | Type   | Description                                                  |
|-----------|--------|--------------------------------------------------------------|
| `query`   | string | ISIN (e.g. `"US0378331005"`), ticker (`"AAPL"`), or name.   |

Searches the securities database for matching instruments.

**Returns:**

```json
{
  "items": [
    {
      "id": "uuid",
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "isin": "US0378331005",
      "security_type": "stock",
      "latest_price": 150.00,
      "latest_price_currency": "USD"
    }
  ],
  "total": 1
}
```

## Integration Examples

### Hermes Agent

Add to your Hermes Agent configuration or `.env`:

```yaml
# In config.yaml under the mcp_servers section:
mcp_servers:
  finance-sync:
    transport: sse
    url: http://localhost:8100/sse
    headers:
      Authorization: "Bearer <your-jwt-token>"
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "finance-sync": {
      "type": "sse",
      "url": "http://localhost:8100/sse",
      "headers": {
        "Authorization": "Bearer <your-jwt-token>"
      }
    }
  }
}
```

### Cursor

In Cursor settings → Features → MCP Servers:

```
Name:    finance-sync
Type:    sse
URL:     http://localhost:8100/sse
Headers: {"Authorization": "Bearer <your-jwt-token>"}
```

### MCP CLI (testing)

```bash
mcp connect http://localhost:8100/sse \
  --header "Authorization: Bearer <jwt-token>"
```

## Integration with the REST API

The MCP server is **not** a replacement for the REST API. It provides
a convenient, read-optimised interface for AI assistants. For advanced
queries (pagination, filtering, historical data exports), use the main
REST API at `/api/v1/`.

## Implementation Details

### Transport

The MCP server uses **SSE (Server-Sent Events)** transport for HTTP
compatibility:

- **SSE endpoint**: `GET /sse` — establishes the event stream
- **Message endpoint**: `POST /messages/` — sends tool/resource requests
- Both endpoints require authentication

### Session Management

Each MCP resource or tool call creates a fresh database session via the
DI Container and closes it after the operation completes. Long-lived
SSE connections do not hold database sessions.

### Security

- Authentication uses the same JWT signing key and API-key bcrypt hashes
  as the REST API
- All credentials are envelope-encrypted with AES-256-GCM at rest, same
  as the main application
- No credentials are ever exposed in MCP responses
- Rate limiting is inherited from the database layer

## Health Check

The MCP server reuses the main app's health check infrastructure. Hit
the root path of the server:

```bash
curl -X POST http://localhost:8100/messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"resources/list","params":{}}'
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| 401 on connect | Missing/invalid auth | Check JWT token or API key |
| 401 on /messages | Token not forwarded | Client must send auth on every POST |
| "No credentials found" | Credentials not stored | Add credentials via REST API |
| "AI summaries disabled" | AI_ENABLED=false | Set AI_ENABLED=true and AI_API_KEY |
| Slow responses | DB queries across many accounts | Use REST API for complex queries |
