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
from datetime import UTC, datetime, timedelta
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
        "financial summaries, AI-powered briefings, and resolving "
        "security identifiers."
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


# ── Parameterised resources ─────────────────────────────────────────────


@mcp.resource(
    "finance://account/{account_id}",
    name="account_detail",
    title="Account Detail",
    description="Detailed information about a single financial account.",
    mime_type="application/json",
)
async def resource_account_detail(ctx: Context, account_id: str) -> str:
    """Return detailed info about a single account.

    URI: ``finance://account/{id}``

    Returns a JSON object with account details including balance,
    type, and metadata.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_account(
            tenant_id, account_id=account_id
        )
        if result is None:
            return _serialise({"error": f"Account {account_id!r} not found"})
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://account/{account_id}/transactions",
    name="account_transactions",
    title="Account Transactions",
    description="Recent transactions for a single account.",
    mime_type="application/json",
)
async def resource_account_transactions(ctx: Context, account_id: str) -> str:
    """Return recent transactions for a single account.

    URI: ``finance://account/{account_id}/transactions``

    Returns a JSON array of the 50 most recent transactions
    for the specified account.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.list_account_transactions(
            tenant_id, account_id=account_id, limit=50
        )
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://portfolio/history",
    name="portfolio_history",
    title="Portfolio History",
    description="Portfolio value over time (daily aggregation).",
    mime_type="application/json",
)
async def resource_portfolio_history(ctx: Context) -> str:
    """Return portfolio value over time.

    URI: ``finance://portfolio/history``

    Returns a JSON array of daily portfolio values showing how
    the total investment value has changed over time.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_portfolio_history(tenant_id, limit=90)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.resource(
    "finance://net-worth/history",
    name="net_worth_history",
    title="Net Worth History",
    description="Net worth over time "
    "(daily aggregation using balance snapshots).",
    mime_type="application/json",
)
async def resource_net_worth_history(ctx: Context) -> str:
    """Return net worth over time.

    URI: ``finance://net-worth/history``

    Returns a JSON array of daily net worth entries (total assets,
    total liabilities, net worth).
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_net_worth_history(tenant_id, limit=90)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


# ═════════════════════════════════════════════════════════════════════════
# Tools — existing
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
# Tools — new AI / data tools
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="get_daily_briefing",
    title="Get Daily Financial Briefing",
    description=(
        "Generate an AI-powered daily financial briefing "
        "covering spending since yesterday, net worth changes, "
        "portfolio highlights, and any unusual activity. "
        "Requires the AI provider to be configured (AI_ENABLED=true)."
    ),
)
async def tool_get_daily_briefing(
    ctx: Context,
    force_refresh: bool = False,
) -> str:
    """Generate a daily financial briefing."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.ai_summary import AISummaryService as _AiSvc

    async with container.session_factory() as session:
        ai_service = _AiSvc(session=session, settings=container.settings)
        try:
            if not container.settings.ai_enabled:
                return _serialise(
                    {"error": "AI summaries are disabled (AI_ENABLED=false)"}
                )

            response = await ai_service.generate_daily_briefing(
                tenant_id, force_refresh=force_refresh
            )
            return _serialise(response.to_dict())
        finally:
            await ai_service.close()


@mcp.tool(
    name="get_subscriptions",
    title="Get Detected Subscriptions",
    description=(
        "Analyse recent transaction history to detect recurring "
        "payments (subscriptions).  Returns a list of detected "
        "candidates with merchant, amount, frequency, and confidence. "
        "Requires the AI provider to be configured."
    ),
)
async def tool_get_subscriptions(
    ctx: Context,
    force_refresh: bool = False,
) -> str:
    """Detect recurring payments from transaction history."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.ai_summary import AISummaryService as _AiSvc

    async with container.session_factory() as session:
        ai_service = _AiSvc(session=session, settings=container.settings)
        try:
            if not container.settings.ai_enabled:
                return _serialise(
                    {"error": "AI summaries are disabled (AI_ENABLED=false)"}
                )

            response = await ai_service.get_subscriptions(
                tenant_id, force_refresh=force_refresh
            )
            return _serialise(response.to_dict())
        finally:
            await ai_service.close()


@mcp.tool(
    name="get_performance",
    title="Get Portfolio Performance",
    description=(
        "Compute portfolio performance returns over time. "
        "Supports different periods and granularity.  Returns "
        "time-series of portfolio value with period-over-period "
        "return percentages."
    ),
)
async def tool_get_performance(
    ctx: Context,
    period: str | None = None,
    granularity: str = "1d",
    currency: str = "EUR",
) -> str:
    """Compute portfolio performance returns."""
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_performance(
            tenant_id,
            period=period,
            granularity=granularity,
            currency=currency,
        )
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.tool(
    name="get_allocation",
    title="Get Portfolio Allocation",
    description=(
        "Get portfolio allocation breakdown by asset class "
        "(security type).  Returns category-level breakdown "
        "with percentages and values."
    ),
)
async def tool_get_allocation(
    ctx: Context,
    by: str = "asset_class",
) -> str:
    """Get portfolio allocation breakdown."""
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.get_allocation(tenant_id, by=by)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.tool(
    name="get_cashflow",
    title="Get Cashflow Summary",
    description=(
        "Aggregate income and expenses by transaction type "
        "for a specified period.  Returns category-level "
        "breakdown with total income, expenses, and net."
    ),
)
async def tool_get_cashflow(
    ctx: Context,
    period: str | None = None,
) -> str:
    """Aggregate income and expenses."""
    tenant_id = _get_tenant_id(ctx)
    read_service = _get_read_service(ctx)
    try:
        # Compute sensible default date range if no period given
        date_to = datetime.now(UTC)
        if not period:
            date_from = date_to - timedelta(days=30)
        elif period.endswith("d"):
            date_from = date_to - timedelta(days=int(period[:-1]))
        elif period.endswith("m"):
            date_from = date_to - timedelta(days=int(period[:-1]) * 30)
        elif period.endswith("y"):
            date_from = date_to - timedelta(days=int(period[:-1]) * 365)
        else:
            date_from = date_to - timedelta(days=30)

        result = await read_service.get_cashflow(
            tenant_id,
            date_from=date_from,
            date_to=date_to,
            period=period,
        )
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()  # noqa: SLF001


@mcp.tool(
    name="list_sync_runs",
    title="List Sync Runs",
    description=(
        "List recent sync run history with optional filtering "
        "by connector and status.  Returns sync runs with "
        "start/completion times, items processed, and errors."
    ),
)
async def tool_list_sync_runs(
    ctx: Context,
    limit: int = 20,
    connector: str | None = None,
    status: str | None = None,
) -> str:
    """List recent sync run history."""
    read_service = _get_read_service(ctx)
    try:
        result = await read_service.list_sync_runs(
            limit=limit,
            connector=connector,
            status=status,
        )
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
