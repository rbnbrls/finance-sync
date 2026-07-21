"""MCP (Model Context Protocol) server for finance-sync.

Exposes financial data as MCP resources and actions as MCP tools,
enabling LLMs (Hermes Agent, Claude Desktop, Cursor, etc.) to query
financial data directly.

Resources
---------
- finance://accounts                 — account list with balances
- finance://portfolio                — current portfolio breakdown
- finance://transactions             — recent transactions
- finance://net-worth                — current net worth
- finance://account/{account_id}                 — single account detail
- finance://account/{account_id}/transactions — transactions for an account
- finance://portfolio/history        — portfolio value over time
- finance://net-worth/history        — net worth over time

Tools
-----
- run_sync            — trigger a manual sync for a connector
- get_summary         — AI-powered summary of recent activity
- resolve_security    — search/lookup a security by ISIN/ticker/name
- get_daily_briefing  — AI-powered daily financial briefing
- get_subscriptions   — detect recurring payments
- get_performance     — portfolio performance returns
- get_allocation      — portfolio allocation breakdown
- get_cashflow        — income/expense cashflow summary
- list_sync_runs      — sync run history
"""

from __future__ import annotations
