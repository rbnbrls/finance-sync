"""MCP (Model Context Protocol) server for finance-sync.

Exposes financial data as MCP resources and actions as MCP tools,
enabling LLMs (Hermes Agent, Claude Desktop, Cursor, etc.) to query
financial data directly.

Resources
---------
- finance://accounts      — account list with balances
- finance://portfolio     — current portfolio breakdown
- finance://transactions  — recent transactions
- finance://net-worth     — current net worth

Tools
-----
- run_sync          — trigger a manual sync for a connector
- get_summary       — AI-powered summary of recent activity
- resolve_security  — search/lookup a security by ISIN/ticker/name
"""

from __future__ import annotations
