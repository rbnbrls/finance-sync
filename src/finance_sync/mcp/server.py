import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
try:
    # Try mcp 2.0.0+ structure
    from mcp.server import FastMCP, Context
except ImportError:
    # Fall back to mcp 1.x structure
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

    The service is constructed with the principal's read scope so the
    tenant's single-owner account scope is enforced.
    """
    from finance_sync.services.read_api import ReadService

    container = _get_container(ctx)
    session = container.session_factory()
    scope = await _get_read_scope(ctx)
    return ReadService(session, scope=scope)


async def _get_read_scope(ctx: ServerContext) -> Any:
    """Resolve the account read scope for the MCP principal.

    JWT principals get the user scope (the tenant's sole owner reads every
    account); API-key principals get the machine scope (their account
    allowlist when set, otherwise the whole tenant datalake).
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
    # Machine scope (API key) or unknown user → tenant datalake scope.
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