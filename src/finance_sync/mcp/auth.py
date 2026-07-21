"""Authentication middleware for the MCP server.

Supports two authentication modes (same as the main REST API):

1. **JWT Bearer token** — ``Authorization: Bearer ***
2. **API key** — ``X-API-Key: ***

The auth context (tenant_id, principal_id) is stored in a ``ContextVar``
so that MCP resource/tool handlers can access it regardless of how the
FastMCP SSE transport constructs the ``RequestContext``.
"""

from __future__ import annotations

import contextvars
from json import dumps as json_dumps
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from jose import JWTError
from starlette.status import HTTP_401_UNAUTHORIZED

from finance_sync.services.auth import (
    decode_token,
    verify_api_key,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from finance_sync.config.settings import Settings


# ── Constants ────────────────────────────────────────────────────────────

_MISSING_AUTH = (
    "Missing or invalid authentication. "
    "Provide Authorization: Bearer *** or X-API-Key header."
)


# ── Auth context ────────────────────────────────────────────────────────


class MCPAuthContext:
    """Holds the resolved authentication principal."""

    def __init__(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        auth_method: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.auth_method = auth_method


# ── Context variable (propagates auth through ASGI/SSE boundary) ───────

_auth_context_var: contextvars.ContextVar[MCPAuthContext] = (
    contextvars.ContextVar("mcp_auth_context")
)


def get_mcp_auth_context() -> MCPAuthContext:
    """Return the ``MCPAuthContext`` for the current request.

    Raises ``RuntimeError`` if no auth context has been set (request
    bypassed the middleware).
    """
    ctx: MCPAuthContext | None = _auth_context_var.get(None)
    if ctx is None:
        msg = _MISSING_AUTH
        raise RuntimeError(msg)
    return ctx


# ── Container cache ────────────────────────────────────────────────────

_global_container: Any = None


async def _get_container(settings: Settings) -> Any:
    """Lazy-init and return the DI container."""
    global _global_container
    if _global_container is None:
        from finance_sync.container import Container

        _global_container = Container.from_settings(settings)
    return _global_container


# ── Middleware ────────────────────────────────────────────────────────────


class MCPAuthMiddleware:
    """ASGI middleware that validates JWT Bearer or API-key auth.

    A pure ASGI middleware (not Starlette BaseHTTPMiddleware) so it
    does not buffer SSE streaming responses.

    The resolved auth context is stored in **both** ``scope["state"]``
    (for Starlette routes that can reach ``request.state``) and a
    ``ContextVar`` (for MCP resource/tool handlers that run inside the
    SSE message-processing loop).
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return None

        auth_ctx = await self._resolve_auth(scope)

        if auth_ctx is None:
            return await self._send_401(send)

        # Store auth in scope state for Starlette routes
        state: dict[str, Any] = scope.setdefault("state", {})
        state["auth"] = auth_ctx

        # Also propagate via ContextVar so MCP handlers inside the SSE
        # message-processing loop can pick it up (the FastMCP SSE transport
        # does NOT set ``RequestContext.request`` to the Starlette Request).
        token = _auth_context_var.set(auth_ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _auth_context_var.reset(token)

        return None

    async def _resolve_auth(self, scope: Scope) -> MCPAuthContext | None:
        """Try JWT, API key, and query-param auth in order."""
        headers = {
            k.decode("ascii", errors="replace").lower(): v.decode(
                "ascii", errors="replace"
            )
            for k, v in scope.get("headers", [])
        }

        # 1. JWT Bearer
        auth_header = headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                payload: dict[str, Any] = decode_token(token, self._settings)
                if payload.get("type") == "access":
                    user_id: str | None = payload.get("sub")
                    tenant_id: str | None = payload.get("tenant_id")
                    if user_id and tenant_id:
                        return MCPAuthContext(
                            tenant_id=tenant_id,
                            principal_id=user_id,
                            auth_method="jwt",
                        )
            except JWTError:
                pass

        # 2. API key
        api_key = headers.get("x-api-key", "")
        if api_key and len(api_key) >= 8:
            try:
                container = await _get_container(self._settings)
                from sqlalchemy import select as _sl

                from finance_sync.models.api_key import ApiKey as _Ak

                async with container.session_factory() as session:
                    prefix = api_key[:8]
                    result = await session.execute(
                        _sl(_Ak).where(
                            _Ak.key_prefix == prefix,
                            _Ak.is_active == True,  # noqa: E712
                        )
                    )
                    key_row = result.scalar_one_or_none()
                    if key_row and verify_api_key(api_key, key_row.key_hash):
                        key_row.last_used_at = (  # type: ignore[attr-defined]
                            __import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc
                            )
                        )
                        await session.flush()
                        return MCPAuthContext(
                            tenant_id=str(key_row.tenant_id),
                            principal_id=str(key_row.id),
                            auth_method="api_key",
                        )
            except Exception:
                pass

        # 3. Query-param token (convenience for SSE clients)
        qs = parse_qs(scope.get("query_string", b"").decode())
        query_token_list = qs.get("access_token", [])
        if query_token_list:
            try:
                payload = decode_token(query_token_list[0], self._settings)
                if payload.get("type") == "access":
                    user_id = payload.get("sub")
                    tenant_id = payload.get("tenant_id")
                    if user_id and tenant_id:
                        return MCPAuthContext(
                            tenant_id=tenant_id,
                            principal_id=user_id,
                            auth_method="jwt_query",
                        )
            except JWTError:
                pass

        return None

    async def _send_401(self, send: Send) -> None:
        """Send a 401 JSON response."""
        body = json_dumps({"detail": _MISSING_AUTH}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": HTTP_401_UNAUTHORIZED,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )


# ── Helper to extract auth context from scope ───────────────────────────

_NOT_AUTHENTICATED = "Not authenticated"


def get_auth_context(scope: Scope) -> MCPAuthContext:
    """Extract the ``MCPAuthContext`` from a request scope.

    Raises ``RuntimeError`` if auth was not set (middleware was bypassed).
    """
    state: dict[str, Any] = getattr(scope, "state", {})
    ctx: MCPAuthContext | None = state.get("auth")
    if ctx is None:
        raise RuntimeError(_NOT_AUTHENTICATED)
    return ctx
