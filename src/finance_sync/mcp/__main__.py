"""Standalone entry point for the finance-sync MCP server.

Usage
-----
Run via ``uv``::

    uv run python -m finance_sync.mcp

Or directly with ``uvicorn``::

    uvicorn finance_sync.mcp.server:app --host 0.0.0.0 --port 8100

Run with stdio transport (for MCP CLI / IDE integration)::

    mcp run src/finance_sync/mcp/server.py
"""

from __future__ import annotations

import uvicorn

from finance_sync.config.settings import Settings


def main() -> None:
    """Start the MCP SSE server."""
    settings = Settings()
    host = getattr(settings, "mcp_host", "0.0.0.0")
    port = int(getattr(settings, "mcp_port", "8100"))

    uvicorn.run(
        "finance_sync.mcp.server:app",
        host=host,
        port=port,
        reload=settings.is_debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
