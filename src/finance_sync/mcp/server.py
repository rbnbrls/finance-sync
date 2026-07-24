"""FastMCP server for finance-sync.

Exposes financial data and actions via the Model Context Protocol (MCP)
using Server-Sent Events (SSE) transport.

Start the server::

    mcp run finance_sync/mcp/server.py  # dev stdio mode
    python -m finance_sync.mcp           # production SSE mode

FastMCP resource & tool implementations that wrap the finance-sync
domain services (ReadService, AISummaryService, SyncOrchestrator, etc.)
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from finance_sync.config.settings import Settings
from finance_sync.container import Container

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def mcp_lifespan(
    _server: FastMCP[Any],
) -> AsyncIterator[dict[str, Any]]:
    """FastMCP lifespan: initialise the DI container.

    Stores the container in lifespan context so resources/tools can
    access it via ``ctx.request.app.state.container``.
    """
    settings = Settings()
    container = Container.from_settings(settings)
    async with container.dispose():
        yield {"container": container, "settings": settings}


# ── MCP Server instance ─────────────────────────────────────────────────

mcp = FastMCP(
    name="finance-sync",
    instructions=(
        "MCP server for the finance-sync financial data platform. "
        "Provides read-only access to accounts, portfolio, transactions, "
        "and net worth data.  Tools allow triggering syncs, querying "
        "financial summaries, and resolving security identifiers."
    ),
    lifespan=mcp_lifespan,
    host="0.0.0.0",
    port=8100,
    sse_path="/sse",
    message_path="/messages/",
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_container(ctx: Context) -> Container:
    """Extract the DI container from FastMCP lifespan context."""
    lifespan_data: dict[str, Any] = ctx.request_context.lifespan_context
    return lifespan_data["container"]


def _get_read_service(ctx: Context) -> Any:
    """Create a ``ReadService`` scoped to the current request's session."""
    from finance_sync.services.read_api import ReadService

    container = _get_container(ctx)
    session = container.session_factory()
    return ReadService(session)


def _get_tenant_id(_ctx: Context) -> str:
    """Extract tenant ID from the authenticated request.

    Reads the auth context from the ``ContextVar`` set by
    ``MCPAuthMiddleware`` (the FastMCP SSE transport does *not* set
    ``RequestContext.request`` to a Starlette ``Request``, so the auth
    state from the ASGI scope must be propagated via a context variable).
    """
    from finance_sync.mcp.auth import get_mcp_auth_context

    auth = get_mcp_auth_context()
    return auth.tenant_id


def _serialise(obj: Any) -> str:
    """JSON-serialise an object, converting non-serialisable types."""
    return json.dumps(obj, indent=2, default=str)


# ═════════════════════════════════════════════════════════════════════════
# Resources
# ═════════════════════════════════════════════════════════════════════════


@mcp.resource(
    "finance://accounts",
    name="accounts",
    title="Account List",
    description="List of all financial accounts with current balances.",
    mime_type="application/json",
)
async def resource_accounts(ctx: Context) -> str:
    """Return all accounts for the authenticated tenant.

    URI: ``finance://accounts``

    Returns a JSON array of accounts with id, name, type, currency,
    and current balance.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.list_accounts(tenant_id, limit=200)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://portfolio",
    name="portfolio",
    title="Portfolio Breakdown",
    description="Current investment portfolio with holdings per account.",
    mime_type="application/json",
)
async def resource_portfolio(ctx: Context) -> str:
    """Return the current portfolio breakdown.

    URI: ``finance://portfolio``

    Returns a JSON object with per-account holdings breakdown,
    including quantities, market values, cost basis, and unrealised P&L.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_portfolio(tenant_id)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://transactions",
    name="transactions",
    title="Recent Transactions",
    description="Recent financial transactions across all accounts.",
    mime_type="application/json",
)
async def resource_transactions(ctx: Context) -> str:
    """Return recent transactions.

    URI: ``finance://transactions``

    Returns a JSON array of the 50 most recent transactions.
    For advanced filtering use the REST API at ``/api/v1/``.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        accts_result = await read_service.list_accounts(tenant_id, limit=100)
        all_txns: list[dict[str, Any]] = []
        for acct in accts_result.items:
            tx_result = await read_service.list_account_transactions(
                tenant_id, acct.id, limit=20
            )
            for tx in tx_result.items:
                d = tx.model_dump()
                d["account_name"] = acct.name
                d["account_type"] = acct.account_type
                all_txns.append(d)
        all_txns.sort(key=lambda t: t.get("occurred_at") or "", reverse=True)
        return _serialise(all_txns[:50])
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://net-worth",
    name="net_worth",
    title="Net Worth",
    description="Current net worth (total assets minus liabilities).",
    mime_type="application/json",
)
async def resource_net_worth(ctx: Context) -> str:
    """Return the current net worth.

    URI: ``finance://net-worth``

    Returns a JSON object with total_assets, total_liabilities,
    net_worth, and per-account breakdown.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_net_worth(tenant_id)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


# ═════════════════════════════════════════════════════════════════════════
# Tools
# ═════════════════════════════════════════════════════════════════════════


class RunSyncInput(BaseModel):
    """Input for ``run_sync`` tool."""

    connector_type: str = Field(
        description=("Connector/provider to sync, e.g. 'bunq', 'trading212'")
    )


@mcp.tool(
    name="run_sync",
    title="Run Financial Sync",
    description=(
        "Trigger a manual sync for a given connector type "
        "(e.g. 'bunq', 'trading212').  Fetches the latest accounts "
        "and transactions from the financial provider."
    ),
)
async def tool_run_sync(ctx: Context, connector_type: str) -> str:
    """Trigger a manual sync for a connector."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)
    settings = container.settings

    # ── Fetch credentials for this provider ─────────────────────────
    from sqlalchemy import select as _sl

    from finance_sync.connectors.models import ConnectorConfig as _Cfg
    from finance_sync.models.credential import Credential as _Cred
    from finance_sync.services.auth import decrypt_credential as _decrypt

    async with container.session_factory() as session:
        result = await session.execute(
            _sl(_Cred).where(
                _Cred.tenant_id == tenant_id,  # type: ignore[attr-defined]
                _Cred.provider_key == connector_type,  # type: ignore[attr-defined]
            )
        )
        cred_row: _Cred | None = result.scalar_one_or_none()  # type: ignore[assignment]

    if cred_row is None:
        msg = (
            f"No credentials found for connector {connector_type!r} "
            f"and tenant {tenant_id}"
        )
        return _serialise({"status": "error", "error": msg})

    raw_payload = _decrypt(
        cred_row.encrypted_payload,
        cred_row.nonce,
        settings,
    )
    try:
        cred_dict: dict[str, str] = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        cred_dict = {"api_key": raw_payload}

    config = _Cfg(
        provider_type=connector_type,
        credentials=cred_dict,
    )

    # ── Run the sync ────────────────────────────────────────────────
    from finance_sync.connectors.registry import ConnectorRegistry as _Reg
    from finance_sync.sync.orchestrator import SyncOrchestrator as _Sync

    registry = _Reg()
    orchestrator = _Sync(
        session_factory=container.session_factory,
        registry=registry,
        tenant_id=tenant_id,
        settings=container.settings,
    )

    result = await orchestrator.run_sync(
        provider_type=connector_type,
        config=config,
    )

    return _serialise(
        {
            "status": str(result.status.value),
            "accounts_synced": result.accounts_synced,
            "transactions_synced": result.transactions_synced,
            "error_message": result.error_message,
            "duration_s": result.duration_s,
        }
    )


class GetSummaryInput(BaseModel):
    """Input for ``get_summary`` tool."""

    timeframe: str = Field(
        default="30d",
        description=(
            "Time period for the summary, e.g. '7d', '30d', '90d'. "
            "Specify number of days followed by 'd'."
        ),
    )


@mcp.tool(
    name="get_summary",
    title="Get Financial Summary",
    description=(
        "Generate an AI-powered natural language summary of recent "
        "financial activity.  Requires the AI provider to be configured "
        "(AI_ENABLED=true, AI_API_KEY set)."
    ),
)
async def tool_get_summary(ctx: Context, timeframe: str = "30d") -> str:
    """Generate an AI-powered summary of recent financial activity."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    days = int(timeframe[:-1]) if timeframe.endswith("d") else 30

    from finance_sync.services.ai_summary import AISummaryService as _AiSvc

    async with container.session_factory() as session:
        ai_service = _AiSvc(session=session, settings=container.settings)
        try:
            if not container.settings.ai_enabled:
                return _serialise(
                    {
                        "error": "AI summaries are disabled (AI_ENABLED=false)",
                    }
                )

            response = await ai_service.generate_summary(
                tenant_id, time_period_days=days
            )
            return _serialise(response.to_dict())
        finally:
            await ai_service.close()


class ResolveSecurityInput(BaseModel):
    """Input for ``resolve_security`` tool."""

    query: str = Field(
        description=(
            "Search query: ISIN (e.g. 'US0378331005'), ticker symbol "
            "(e.g. 'AAPL'), or instrument name (e.g. 'Apple Inc.')"
        )
    )


@mcp.tool(
    name="resolve_security",
    title="Resolve Security",
    description=(
        "Search or resolve a financial security by ISIN, ticker, "
        "or name.  Returns matching canonical security records "
        "with identifiers and latest price."
    ),
)
async def tool_resolve_security(ctx: Context, query: str) -> str:
    """Search/lookup a security by ISIN, ticker, or name."""
    container = _get_container(ctx)

    async with container.session_factory() as session:
        from finance_sync.services.read_api import ReadService

        read_service = ReadService(session)
        try:
            result = await read_service.list_securities(search=query, limit=20)
            return _serialise(result.model_dump())
        finally:
            await read_service._session.aclose()  # noqa: SLF001


# ═════════════════════════════════════════════════════════════════════════
# ASGI app factory
# ═════════════════════════════════════════════════════════════════════════


def create_sse_app() -> Any:
    """Build the ASGI app with auth middleware.

    Returns a fully configured ASGI application that
    serves the MCP SSE endpoint at ``/sse`` with authentication.

    Usage::

        uvicorn finance_sync.mcp.server:app --host 0.0.0.0 --port 8100
    """
    from starlette.applications import Starlette as _Starlette
    from starlette.routing import Mount

    from finance_sync.mcp.auth import MCPAuthMiddleware

    # Get the raw SSE app from FastMCP
    raw_sse = mcp.sse_app(mount_path="/")

    # Get settings for the middleware
    settings = Settings()
    app = _Starlette(
        debug=settings.is_debug,
        routes=[
            Mount("/", app=raw_sse),
        ],
    )
    # Wrap the entire stack with auth middleware (ASGI-level, no buffering)
    auth_mw: Any = MCPAuthMiddleware(app, settings=settings)
    return auth_mw


# ═════════════════════════════════════════════════════════════════════════
# Module-level app instance (for uvicorn / ASGI)
# ═════════════════════════════════════════════════════════════════════════

app = create_sse_app()
