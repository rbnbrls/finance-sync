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
from mcp.server.session import ServerSession
from pydantic import BaseModel, Field

from finance_sync.config.settings import Settings
from finance_sync.container import Container

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = structlog.get_logger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def mcp_lifespan(
    _server: FastMCP[Any],
) -> AsyncGenerator[dict[str, Any]]:
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

# FastMCP is typed as FastMCP[LifespanResultT]; our lifespan yields
# dict[str, Any], so tool/resource Context carries that lifespan payload.
ServerContext = Context[ServerSession, dict[str, Any]]


def _get_container(ctx: ServerContext) -> Container:
    """Extract the DI container from FastMCP lifespan context."""
    lifespan_data: dict[str, Any] = ctx.request_context.lifespan_context
    return lifespan_data["container"]


async def _get_read_service(ctx: ServerContext) -> Any:
    """Create a ``ReadService`` scoped to the current request's session.

    The service is constructed with the principal's household
    visibility scope so private accounts of other household members are
    never readable through MCP tools/resources.
    """
    from finance_sync.services.read_api import ReadService

    container = _get_container(ctx)
    session = container.session_factory()
    scope = await _get_read_scope(ctx)
    return ReadService(session, scope=scope)


async def _get_read_scope(ctx: ServerContext) -> Any:
    """Resolve the household visibility scope for the MCP principal.

    JWT principals get their user scope (household + own private +
    admin unowned); API-key principals get the machine scope (household
    + system-owned only).
    """
    from sqlalchemy import select

    from finance_sync.mcp.auth import get_mcp_auth_context
    from finance_sync.models.user import User
    from finance_sync.services.visibility import ReadScope

    auth = get_mcp_auth_context()
    if auth.auth_method == "jwt":
        container = _get_container(ctx)
        async with container.session_factory() as session:
            result = await session.execute(
                select(User).where(
                    User.id == auth.principal_id,  # type: ignore[attr-defined]
                    User.is_active.is_(True),  # type: ignore[attr-defined]
                )
            )
            user = result.scalar_one_or_none()
            if user is not None:
                return ReadScope.for_user(user)
    # Machine scope (API key) or unknown user → household + system-owned.
    return ReadScope.for_api_key(auth.tenant_id)


def _get_tenant_id(_ctx: ServerContext) -> str:
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
async def resource_accounts(ctx: ServerContext) -> str:
    """Return all accounts for the authenticated tenant.

    URI: ``finance://accounts``

    Returns a JSON array of accounts with id, name, type, currency,
    and current balance.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
    try:
        result = await read_service.list_accounts(tenant_id, limit=200)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()


@mcp.resource(
    "finance://portfolio",
    name="portfolio",
    title="Portfolio Breakdown",
    description="Current investment portfolio with holdings per account.",
    mime_type="application/json",
)
async def resource_portfolio(ctx: ServerContext) -> str:
    """Return the current portfolio breakdown.

    URI: ``finance://portfolio``

    Returns a JSON object with per-account holdings breakdown,
    including quantities, market values, cost basis, and unrealised P&L.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
    try:
        result = await read_service.get_portfolio(tenant_id)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()


@mcp.resource(
    "finance://transactions",
    name="transactions",
    title="Recent Transactions",
    description="Recent financial transactions across all accounts.",
    mime_type="application/json",
)
async def resource_transactions(ctx: ServerContext) -> str:
    """Return recent transactions.

    URI: ``finance://transactions``

    Returns a JSON array of the 50 most recent transactions.
    For advanced filtering use the REST API at ``/api/v1/``.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
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
        await read_service._session.aclose()


@mcp.resource(
    "finance://net-worth",
    name="net_worth",
    title="Net Worth",
    description="Current net worth (total assets minus liabilities).",
    mime_type="application/json",
)
async def resource_net_worth(ctx: ServerContext) -> str:
    """Return the current net worth.

    URI: ``finance://net-worth``

    Returns a JSON object with total_assets, total_liabilities,
    net_worth, and per-account breakdown.
    """
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
    try:
        result = await read_service.get_net_worth(tenant_id)
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()


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
async def tool_run_sync(ctx: ServerContext, connector_type: str) -> str:
    """Trigger a manual sync for a connector (all of its connections)."""
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
        cred_rows = list(result.scalars().all())

    if not cred_rows:
        msg = (
            f"No credentials found for connector {connector_type!r} "
            f"and tenant {tenant_id}"
        )
        return _serialise({"status": "error", "error": msg})

    # ── Run one sync per connection (isolated, never blocks siblings) ─
    from finance_sync.connectors.registry import ConnectorRegistry as _Reg
    from finance_sync.sync.orchestrator import SyncOrchestrator as _Sync

    registry = _Reg()
    outcomes: list[dict[str, object]] = []

    for cred_row in cred_rows:
        connection_id = str(cred_row.id)
        # Paused connections are skipped by automated syncs; a manual
        # MCP trigger is explicit, so it still runs them.
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
            connection_id=connection_id,
            selected_accounts=list(cred_row.selected_accounts or []),
        )

        orchestrator = _Sync(
            session_factory=container.session_factory,
            registry=registry,
            tenant_id=tenant_id,
            settings=container.settings,
        )
        try:
            result = await orchestrator.run_sync(
                provider_type=connector_type,
                config=config,
                connection_id=connection_id,
                selected_accounts=list(cred_row.selected_accounts or []),
            )
            outcomes.append(
                {
                    "connection_id": connection_id,
                    "status": str(result.status.value),
                    "accounts_synced": result.accounts_synced,
                    "transactions_synced": result.transactions_synced,
                    "holdings_synced": result.holdings_synced,
                    "unresolved_securities": result.unresolved_securities,
                    "error_message": result.error_message,
                    "duration_s": result.duration_s,
                }
            )
        except Exception as exc:
            outcomes.append(
                {
                    "connection_id": connection_id,
                    "status": "error",
                    "error_message": str(exc)[:500],
                }
            )

    return _serialise({"status": "completed", "connections": outcomes})


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
async def tool_get_summary(ctx: ServerContext, timeframe: str = "30d") -> str:
    """Generate an AI-powered summary of recent financial activity."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    days = int(timeframe[:-1]) if timeframe.endswith("d") else 30

    from finance_sync.services.ai_summary import AISummaryService as _AiSvc

    async with container.session_factory() as session:
        ai_service = _AiSvc(
            session=session,
            settings=container.settings,
            scope=await _get_read_scope(ctx),
        )
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
async def tool_resolve_security(ctx: ServerContext, query: str) -> str:
    """Search/lookup a security by ISIN, ticker, or name."""
    container = _get_container(ctx)

    async with container.session_factory() as session:
        from finance_sync.services.read_api import ReadService

        read_service = ReadService(session)
        try:
            result = await read_service.list_securities(search=query, limit=20)
            return _serialise(result.model_dump())
        finally:
            await read_service._session.aclose()  # type: ignore[reportPrivateUsage]


# ═════════════════════════════════════════════════════════════════════════
# Tools — phase 3 new tools
# ═════════════════════════════════════════════════════════════════════════


class GetDailyBriefingInput(BaseModel):
    """Input for ``get_daily_briefing`` tool."""

    timeframe: str = Field(
        default="today",
        description=(
            "Time period for the briefing, e.g. 'today', 'week', 'month'."
        ),
    )


@mcp.tool(
    name="get_daily_briefing",
    title="Get Daily Briefing",
    description=(
        "Generate an AI-powered daily briefing covering spending since "
        "yesterday, net worth change, portfolio highlights, and unusual "
        "activity.  Requires AI_ENABLED=true and AI_API_KEY to be set."
    ),
)
async def tool_get_daily_briefing(
    ctx: ServerContext,
    timeframe: str = "today",
) -> str:
    """Generate an AI-powered daily financial briefing."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.ai_summary import AISummaryService as _AiSvc

    async with container.session_factory() as session:
        ai_service = _AiSvc(
            session=session,
            settings=container.settings,
            scope=await _get_read_scope(ctx),
        )
        try:
            if not container.settings.ai_enabled:
                return _serialise(
                    {
                        "error": "AI briefings are disabled (AI_ENABLED=false)",
                    }
                )
            response = await ai_service.generate_daily_briefing(tenant_id)
            return _serialise(response.to_dict())
        finally:
            await ai_service.close()


class GetSubscriptionsInput(BaseModel):
    """Input for ``get_subscriptions`` tool."""

    active_only: bool = Field(
        default=True,
        description="Only return active subscriptions when True.",
    )


@mcp.tool(
    name="get_subscriptions",
    title="Get Subscriptions",
    description=(
        "Detect and return recurring subscription payments "
        "from transaction history, combining merchant classification "
        "and pattern recognition."
    ),
)
async def tool_get_subscriptions(
    ctx: ServerContext, active_only: bool = True
) -> str:
    """Detect recurring subscriptions from transaction history."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.subscription_detector.detector import (
        SubscriptionDetector as _SubDetector,
    )

    scope = await _get_read_scope(ctx)

    detector = _SubDetector(
        session_factory=container.session_factory,
        tenant_id=tenant_id,
    )
    subscriptions = await detector.list_subscriptions(
        status="active" if active_only else None,
        account_ids=scope.account_ids_subquery(),
    )
    return _serialise(
        [
            {
                "id": s.id,
                "merchant_name": s.merchant_name,
                "raw_description": s.raw_description,
                "amount": str(s.amount),
                "currency_code": s.currency_code,
                "frequency_days": s.frequency_days,
                "frequency_label": s.frequency_label,
                "confidence": s.confidence,
                "status": s.status,
                "sector": s.sector,
                "category": s.category,
                "first_detected_at": s.first_detected_at.isoformat()
                if s.first_detected_at
                else None,
                "last_detected_at": s.last_detected_at.isoformat()
                if s.last_detected_at
                else None,
                "occurrence_count": s.occurrence_count,
            }
            for s in subscriptions
        ]
    )


class GetPerformanceInput(BaseModel):
    """Input for ``get_performance`` tool."""

    subject: str = Field(
        default="portfolio",
        description=(
            "Performance subject: 'portfolio', 'account', or a specific "
            "security identifier.  Defaults to the overall portfolio."
        ),
    )
    period: str = Field(
        default="1y",
        description=(
            "Evaluation period, e.g. '1m', '3m', '6m', '1y', 'ytd'.  "
            "Defaults to '1y' (1 year)."
        ),
    )


@mcp.tool(
    name="get_performance",
    title="Get Performance",
    description=(
        "Calculate portfolio performance metrics including "
        "time-weighted return (TWR) for a given period and subject."
    ),
)
async def tool_get_performance(
    ctx: ServerContext,
    subject: str = "portfolio",
    period: str = "1y",
) -> str:
    """Calculate portfolio performance metrics."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.performance import PerformanceService as _Perf

    now = datetime.now(UTC)
    days_map = {
        "1m": 30,
        "3m": 90,
        "6m": 180,
        "1y": 365,
        "2y": 730,
        "ytd": now.timetuple().tm_yday,
    }
    period_days = days_map.get(period, 365)
    date_from = now - timedelta(days=period_days)

    async with container.session_factory() as session:
        svc = _Perf(session)
        result = await svc.calculate_twr(
            tenant_id,
            date_from=date_from,
            date_to=now,
            annualized=True,
        )
        return _serialise(result.model_dump())


class GetAllocationInput(BaseModel):
    """Input for ``get_allocation`` tool."""

    by: str = Field(
        default="asset_class",
        description=(
            "Allocation breakdown dimension: 'asset_class', 'sector', "
            "or 'region'.  Defaults to 'asset_class'."
        ),
    )
    target_currency: str | None = Field(
        default=None,
        description=(
            "Optional ISO-4217 currency code to normalise all values "
            "into a single currency (e.g. 'EUR', 'USD')."
        ),
    )


@mcp.tool(
    name="get_allocation",
    title="Get Allocation",
    description=(
        "Compute portfolio allocation breakdowns by asset class, "
        "sector, or region with optional multi-currency normalisation."
    ),
)
async def tool_get_allocation(
    ctx: ServerContext,
    by: str = "asset_class",
    target_currency: str | None = None,
) -> str:
    """Compute portfolio allocation breakdowns."""
    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)

    from finance_sync.services.allocation import AllocationService as _Alloc

    async with container.session_factory() as session:
        svc = _Alloc(
            session=session,
            fx_service=(
                container.fx_service
                if container.settings.openbb_api_key is not None
                else None
            ),
        )
        result = await svc.get_allocation(
            tenant_id,
            target_currency=target_currency,
        )
        return _serialise(result.model_dump())


class GetCashflowInput(BaseModel):
    """Input for ``get_cashflow`` tool."""

    period: str = Field(
        default="30d",
        description=(
            "Lookback period, e.g. '7d', '30d', '90d', '1y'.  "
            "Defaults to '30d'."
        ),
    )


@mcp.tool(
    name="get_cashflow",
    title="Get Cashflow Summary",
    description=(
        "Return aggregate cashflow (inflows, outflows, net) for "
        "a given period across all accounts."
    ),
)
async def tool_get_cashflow(ctx: ServerContext, period: str = "30d") -> str:
    """Return aggregate cashflow for a given period."""
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
    try:
        days = int(period[:-1]) if period.endswith("d") else 30
        date_from = datetime.now(UTC) - timedelta(days=days)
        result = await read_service.get_cashflow(
            tenant_id,
            date_from=date_from,
        )
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()


class ListSyncRunsInput(BaseModel):
    """Input for ``list_sync_runs`` tool."""

    limit: int = Field(
        default=10,
        description="Maximum number of sync runs to return.",
    )
    connector: str | None = Field(
        default=None,
        description="Optional filter by connector type (e.g. 'bunq').",
    )
    status: str | None = Field(
        default=None,
        description=(
            "Optional filter by run status "
            "(e.g. 'success', 'failed', 'running')."
        ),
    )


@mcp.tool(
    name="list_sync_runs",
    title="List Sync Runs",
    description=(
        "List recent sync runs with status, duration, "
        "and per-connector success/failure counts."
    ),
)
async def tool_list_sync_runs(
    ctx: ServerContext,
    limit: int = 10,
    connector: str | None = None,
    status: str | None = None,
) -> str:
    """List recent sync runs with status summaries."""
    tenant_id = _get_tenant_id(ctx)
    read_service = await _get_read_service(ctx)
    try:
        result = await read_service.list_sync_runs(
            tenant_id,
            limit=limit,
            connector=connector,
            status=status,
        )
        return _serialise(result.model_dump())
    finally:
        await read_service._session.aclose()


# ═════════════════════════════════════════════════════════════════════════
# Tools — market intelligence (source layer)
# ═════════════════════════════════════════════════════════════════════════


class MarketIntelligenceQueryInput(BaseModel):
    """Input for ``list_market_intelligence`` tool."""

    provider: str | None = Field(
        default=None,
        description=(
            "Filter by provider key, e.g. 'sec' (SEC EDGAR public data) "
            "or 'openbb'."
        ),
    )
    kind: str | None = Field(
        default=None,
        description=(
            "Filter by item kind: news_article, corporate_event, "
            "earnings_report, earnings_call_transcript, analyst_estimate."
        ),
    )
    review_required: bool | None = Field(
        default=None,
        description=(
            "Only return items flagged for identity review when True."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of items to return.",
    )


@mcp.tool(
    name="list_market_intelligence",
    title="List Market Intelligence",
    description=(
        "List stored market-intelligence observations (news, corporate "
        "events, earnings, analyst estimates) from the self-hosted "
        "source layer.  Tenant-scoped.  Never returns provider "
        "credentials; restricted items never include full article text."
    ),
)
async def tool_list_market_intelligence(
    ctx: ServerContext,
    provider: str | None = None,
    kind: str | None = None,
    review_required: bool | None = None,
    limit: int = 20,
) -> str:
    """List stored market-intelligence observations for the tenant."""
    from finance_sync.services.market_intelligence_read import (
        MarketIntelligenceReadService as _ReadSvc,
    )

    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)
    session = container.session_factory()
    service = _ReadSvc(session)
    try:
        result = await service.list_items(
            tenant_id,
            provider=provider,
            kind=kind,
            review_required=review_required,
            limit=limit,
        )
        return _serialise(result.model_dump())
    finally:
        await session.aclose()


class ProviderStateInput(BaseModel):
    """Input for ``list_intel_provider_states`` tool."""

    provider: str | None = Field(
        default=None,
        description="Optional provider key filter.",
    )


@mcp.tool(
    name="list_intel_provider_states",
    title="List Intel Provider States",
    description=(
        "Return run/freshness/availability state of every market-"
        "intelligence provider for the tenant, including sanitised last "
        "errors (never credentials) and explicit unavailable statuses."
    ),
)
async def tool_list_intel_provider_states(
    ctx: ServerContext,
    provider: str | None = None,
) -> str:
    """Return per-provider run/freshness state for the tenant."""
    from finance_sync.services.market_intelligence_read import (
        MarketIntelligenceReadService as _ReadSvc,
    )

    tenant_id = _get_tenant_id(ctx)
    container = _get_container(ctx)
    session = container.session_factory()
    service = _ReadSvc(session)
    try:
        states = await service.list_provider_states(tenant_id)
        if provider:
            states = [s for s in states if s.provider == provider]
        return _serialise([s.model_dump() for s in states])
    finally:
        await session.aclose()


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
