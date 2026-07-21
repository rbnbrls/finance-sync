# MCP Server Integration

finance-sync exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io)
server that allows LLMs and AI-powered tools to query your financial data directly.

## Overview

The MCP server provides **resources** (read-only data endpoints) and **tools**
(actions with parameters) that wrap the finance-sync domain services.

### Architecture

```
┌─────────────────────────────┐
│  LLM / AI Client            │
│  (Claude Desktop, Cursor,   │
│   Hermes Agent, etc.)       │
└──────────┬──────────────────┘
           │ SSE / stdio
┌──────────▼──────────────────┐
│  MCPAuthMiddleware           │  ← JWT Bearer / API key validation
├─────────────────────────────┤
│  FastMCP SSE Transport       │  ← /sse, /messages/
├─────────────────────────────┤
│  Resources & Tools            │  ← Wrap ReadService, AISummaryService,
│                              │     SyncOrchestrator, etc.
└─────────────────────────────┘
```

## Running the Server

### Production (SSE mode)

```bash
# Standalone
python -m finance_sync.mcp

# Or with uvicorn directly
uvicorn finance_sync.mcp.server:app --host 0.0.0.0 --port 8100
```

Configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8100` | Listen port |

### Development (stdio mode)

```bash
mcp run src/finance_sync/mcp/server.py
```

## Authentication

The MCP server supports the same authentication mechanisms as the REST API:

1. **JWT Bearer token** — `Authorization: Bearer <token>`
2. **API key** — `X-API-Key: <key>`
3. **Query-param JWT** — `/sse?access_token=<token>` (convenience for SSE clients)

All resources and tools are scoped to the authenticated tenant.

## Resources

Resources are read-only data endpoints that return JSON.

| URI | Name | Description |
|-----|------|-------------|
| `finance://accounts` | accounts | List of all financial accounts with balances |
| `finance://account/{account_id}` | account_detail | Details for a single account |
| `finance://account/{account_id}/transactions` | account_transactions | Recent transactions for an account |
| `finance://portfolio` | portfolio | Current portfolio breakdown by account |
| `finance://portfolio/history` | portfolio_history | Portfolio value over time (90-day) |
| `finance://net-worth` | net_worth | Current net worth (assets − liabilities) |
| `finance://net-worth/history` | net_worth_history | Net worth over time (90-day) |
| `finance://transactions` | transactions | Recent transactions (all accounts, top 50) |

### Example: Reading a resource

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "finance-sync": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/finance-sync",
        "python", "-m", "finance_sync.mcp"
      ],
      "env": {
        "DATABASE_URL": "postgresql+asyncpg://...",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": "8100"
      }
    }
  }
}
```

## Tools

Tools are actions that accept parameters and return results.

| Name | Description | Key Parameters |
|------|-------------|----------------|
| `run_sync` | Trigger a manual sync | `connector_type` (required) |
| `get_summary` | AI-powered financial summary | `timeframe` (default: `30d`) |
| `get_daily_briefing` | AI daily financial briefing | `timeframe`, `force_refresh` |
| `get_subscriptions` | Detect recurring payments | `force_refresh` |
| `get_performance` | Portfolio performance returns | `period`, `granularity`, `currency` |
| `get_allocation` | Portfolio allocation breakdown | `by` (default: `asset_class`) |
| `get_cashflow` | Income/expense summary | `period` |
| `list_sync_runs` | Sync run history | `limit`, `connector`, `status` |
| `resolve_security` | Search/lookup a security | `query` (ISIN, ticker, or name) |

## Client Integration

### Hermes Agent

In Hermes Agent, configure the MCP client:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  finance-sync:
    command: uv
    args:
      - run
      - --directory
      - /path/to/finance-sync
      - python
      - -m
      - finance_sync.mcp
    env:
      DATABASE_URL: "${DATABASE_URL}"
```

### Claude Desktop

```json
{
  "mcpServers": {
    "finance-sync": {
      "command": "uvx",
      "args": ["finance-sync"]
    }
  }
}
```

### Programmatic Client (Python)

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://localhost:8100/sse") as streams:
    async with ClientSession(streams[0], streams[1]) as session:
        # List resources
        resources = await session.list_resources()

        # Read a resource
        accounts = await session.read_resource("finance://accounts")

        # Call a tool
        result = await session.call_tool("get_summary", {
            "timeframe": "30d"
        })
```

## Security

- All endpoints require authentication (no anonymous access)
- API keys can be created/revoked via the REST API at `/api/v1/auth/api-keys`
- The MCP server uses the same API key and JWT validation as the main app
- SSE connections are authenticated at connection time via JWT or API key
- Consider running the MCP server on localhost only in production (`MCP_HOST=127.0.0.1`)

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| 401 Unauthorized | Missing/invalid auth | Set `Authorization` or `X-API-Key` header |
| `No credentials found` | No saved provider creds | Set up connector via API first |
| `AI summaries disabled` | AI not configured | Set `AI_ENABLED=true` and `AI_API_KEY` |
| Connection refused | Wrong port/host | Check `MCP_PORT` and `MCP_HOST` |
