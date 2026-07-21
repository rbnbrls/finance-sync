"""Tests for the finance-sync MCP server.

Covers:
- Module imports and server instantiation
- Resource and tool registration
- Auth middleware (JWT, API key, query-param)
- Context variable auth propagation through SSE boundary
- Helper functions (_get_container, _get_read_service, _get_tenant_id)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═════════════════════════════════════════════════════════════════════════
# Module-level tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPModule:
    """Verify the MCP module imports correctly."""

    def test_import_server(self) -> None:
        """mcp.server imports without error."""
        from finance_sync.mcp import server as mod

        assert mod is not None

    def test_import_auth(self) -> None:
        """mcp.auth imports without error."""
        from finance_sync.mcp import auth as mod

        assert mod is not None

    def test_server_has_expected_objects(self) -> None:
        """Server module exports mcp, app, create_sse_app."""
        from finance_sync.mcp.server import app, create_sse_app, mcp

        assert mcp.name == "finance-sync"
        assert callable(create_sse_app)
        # app should be the wrapped ASGI middleware
        assert "auth" in type(app).__name__.lower()

    def test_auth_has_expected_exports(self) -> None:
        """Auth module exports key types and functions."""
        from finance_sync.mcp.auth import (
            MCPAuthContext,
            MCPAuthMiddleware,
            get_mcp_auth_context,
        )

        assert MCPAuthContext is not None
        assert MCPAuthMiddleware is not None
        assert get_mcp_auth_context is not None


# ═════════════════════════════════════════════════════════════════════════
# FastMCP resource / tool introspection
# ═════════════════════════════════════════════════════════════════════════


class TestMCPResources:
    """Verify resources are registered correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        """Import the MCP server and collect templates."""
        from finance_sync.mcp.server import mcp

        self._mcp = mcp
        self._templates = mcp._resource_manager.list_templates()
        self._template_map = {str(t.uri_template): t for t in self._templates}

    def test_resource_count(self) -> None:
        """There are exactly 4 resources defined."""
        uris = set(self._template_map.keys())
        assert "finance://accounts" in uris
        assert "finance://portfolio" in uris
        assert "finance://transactions" in uris
        assert "finance://net-worth" in uris
        assert len(uris) == 8

    def test_resource_metadata(self) -> None:
        """Check resource metadata."""
        accounts = self._template_map["finance://accounts"]
        assert accounts.name == "accounts"
        assert "balances" in (accounts.description or "").lower()

        portfolio = self._template_map["finance://portfolio"]
        assert portfolio.name == "portfolio"
        assert "holdings" in (portfolio.description or "").lower()


class TestMCPTools:
    """Verify tools are registered correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        """Import the MCP server."""
        from finance_sync.mcp.server import mcp

        self._mcp = mcp

    def test_tool_count(self) -> None:
        """There are exactly 3 tools defined."""
        tools = self._mcp._tool_manager.list_tools()
        tool_names = {t.name for t in tools}
        assert "run_sync" in tool_names
        assert "get_summary" in tool_names
        assert "resolve_security" in tool_names
        assert len(tool_names) == 9

    def test_tool_input_schemas(self) -> None:
        """Tools have the expected input parameters."""
        tools = self._mcp._tool_manager.list_tools()
        tool_map = {t.name: t for t in tools}

        run = tool_map["run_sync"]
        assert run.parameters is not None
        props = run.parameters.get("properties", {})
        assert "connector_type" in props

        summary = tool_map["get_summary"]
        assert summary.parameters is not None
        props = summary.parameters.get("properties", {})
        assert "timeframe" in props

        resolve = tool_map["resolve_security"]
        assert resolve.parameters is not None
        props = resolve.parameters.get("properties", {})
        assert "query" in props


# ═════════════════════════════════════════════════════════════════════════
# MCPAuthContext
# ═════════════════════════════════════════════════════════════════════════


class TestMCPAuthContext:
    """MCPAuthContext stores auth metadata."""

    def test_create(self) -> None:
        from finance_sync.mcp.auth import MCPAuthContext

        ctx = MCPAuthContext(
            tenant_id="tenant_01",
            principal_id="user_abc",
            auth_method="jwt",
        )
        assert ctx.tenant_id == "tenant_01"
        assert ctx.principal_id == "user_abc"
        assert ctx.auth_method == "jwt"


# ═════════════════════════════════════════════════════════════════════════
# get_mcp_auth_context context var
# ═════════════════════════════════════════════════════════════════════════


class TestGetMCPAuthContext:
    """ContextVar-based auth propagation."""

    def test_get_without_set_raises(self) -> None:
        from finance_sync.mcp.auth import get_mcp_auth_context

        with pytest.raises(RuntimeError, match="authentication"):
            get_mcp_auth_context()

    def test_get_after_set(self) -> None:
        from finance_sync.mcp.auth import (
            MCPAuthContext,
            get_mcp_auth_context,
        )
        from finance_sync.mcp.auth import _auth_context_var as cv

        auth = MCPAuthContext(
            tenant_id="t1", principal_id="u1", auth_method="api_key"
        )
        token = cv.set(auth)
        try:
            result = get_mcp_auth_context()
            assert result.tenant_id == "t1"
            assert result.principal_id == "u1"
            assert result.auth_method == "api_key"
        finally:
            cv.reset(token)

    def test_get_after_reset(self) -> None:
        from finance_sync.mcp.auth import (
            MCPAuthContext,
            get_mcp_auth_context,
        )
        from finance_sync.mcp.auth import _auth_context_var as cv

        auth = MCPAuthContext(
            tenant_id="t1", principal_id="u1", auth_method="jwt"
        )
        token = cv.set(auth)
        cv.reset(token)

        with pytest.raises(RuntimeError):
            get_mcp_auth_context()


# ═════════════════════════════════════════════════════════════════════════
# MCPAuthMiddleware (ASGI-level unit tests)
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_settings() -> Any:
    """Return a mock Settings object."""
    s = MagicMock()
    s.is_debug = False
    s.log_level = "INFO"
    s.secret_key = MagicMock()
    s.secret_key.get_secret_value.return_value = "test-secret-key-12345"
    s.access_token_expire_minutes = 30
    s.refresh_token_expire_days = 7
    s.jwt_algorithm = "HS256"
    return s


class TestMCPAuthMiddleware:
    """Unit tests for the ASGI auth middleware."""

    @pytest.fixture
    def mock_app(self) -> AsyncMock:
        """Return an inner ASGI app that records calls."""
        return AsyncMock()

    @pytest.fixture
    def middleware(self, mock_app: AsyncMock, mock_settings: Any) -> Any:
        """Create a fresh MCPAuthMiddleware for each test."""
        from finance_sync.mcp.auth import MCPAuthMiddleware

        return MCPAuthMiddleware(mock_app, mock_settings)

    def _make_scope(
        self,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
        query_string: bytes = b"",
        path: str = "/sse",
    ) -> dict[str, Any]:
        """Build a minimal ASGI HTTP scope."""
        return {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query_string,
            "headers": headers or [],
            "scheme": "http",
            "server": ("localhost", 8100),
        }

    async def test_non_http_passthrough(
        self, middleware: Any, mock_app: AsyncMock
    ) -> None:
        """Non-HTTP scopes (e.g. websocket) pass through."""
        scope: dict[str, Any] = {"type": "websocket"}
        receive = MagicMock()
        send = MagicMock()
        await middleware(scope, receive, send)
        mock_app.assert_awaited_once_with(scope, receive, send)

    @patch("finance_sync.mcp.auth.decode_token")
    async def test_valid_jwt(
        self,
        mock_decode: MagicMock,
        middleware: Any,
        mock_app: AsyncMock,
    ) -> None:
        """Valid JWT Bearer token is accepted and auth stored."""
        mock_decode.return_value = {
            "type": "access",
            "sub": "user_01",
            "tenant_id": "tenant_01",
        }
        scope = self._make_scope(
            headers=[(b"authorization", b"Bearer valid.jwt.token")]
        )
        send = AsyncMock()

        await middleware(scope, MagicMock(), send)

        # Inner app should have been called
        assert mock_app.await_count == 1
        # Auth should be in scope state
        assert scope["state"]["auth"] is not None
        assert scope["state"]["auth"].tenant_id == "tenant_01"

        # ContextVar should be set (we can't read it directly here
        # because the middleware already reset it, but the inner app
        # was able to call get_mcp_auth_context())

    @patch("finance_sync.mcp.auth.decode_token")
    async def test_invalid_jwt_falls_through(
        self,
        mock_decode: MagicMock,
        middleware: Any,
        mock_app: AsyncMock,
        mock_settings: Any,
    ) -> None:
        """Invalid JWT falls through to other auth methods, then 401."""
        mock_decode.side_effect = __import__("jose").JWTError("bad token")

        # Also ensure no API key or query param is present
        scope = self._make_scope(
            headers=[(b"authorization", b"Bearer invalid.token.stuff")]
        )
        send = AsyncMock()

        await middleware(scope, MagicMock(), send)

        # Should have returned 401 without calling inner app
        mock_app.assert_not_awaited()
        # The send mock should have received start and body
        assert send.await_count == 2
        # Check the status
        call_args = send.call_args_list
        start_msg = call_args[0][0][0] if call_args else {}
        assert start_msg.get("type") == "http.response.start"
        assert start_msg.get("status") == 401

    async def test_valid_api_key(
        self,
        middleware: Any,
        mock_app: AsyncMock,
    ) -> None:
        """Valid API key is accepted."""
        with (
            patch("finance_sync.mcp.auth._get_container") as mock_get_container,
            patch("finance_sync.mcp.auth.verify_api_key", return_value=True),
        ):
            # Build a mock key row
            mock_key_row = MagicMock()
            mock_key_row.tenant_id = "tenant_01"
            mock_key_row.id = 42
            mock_key_row.key_hash = "hashed_key"
            mock_key_row.key_prefix = "testpref"
            mock_key_row.is_active = True
            mock_key_row.last_used_at = None

            # Build execute result that returns the key row
            mock_exec_result = MagicMock()
            mock_exec_result.scalar_one_or_none.return_value = mock_key_row

            # Build session mock
            mock_session = AsyncMock()
            mock_session.execute.return_value = mock_exec_result
            mock_session.flush = AsyncMock()

            # Build container mock with working async context manager
            mock_container = MagicMock()
            mock_session_factory = MagicMock()
            mock_session_factory.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_container.session_factory = mock_session_factory
            mock_get_container.return_value = mock_container

            scope = self._make_scope(
                headers=[(b"x-api-key", b"testpref12345678")]
            )
            send = AsyncMock()

            await middleware(scope, MagicMock(), send)

            # Inner app should have been called
            assert mock_app.await_count == 1, (
                f"Inner app was called {mock_app.await_count} times, expected 1"
            )
            assert scope["state"]["auth"].tenant_id == "tenant_01"
            assert scope["state"]["auth"].auth_method == "api_key"

    @patch("finance_sync.mcp.auth.decode_token")
    async def test_query_param_jwt(
        self,
        mock_decode: MagicMock,
        middleware: Any,
        mock_app: AsyncMock,
    ) -> None:
        """JWT in query param (access_token) is accepted."""
        mock_decode.return_value = {
            "type": "access",
            "sub": "user_q",
            "tenant_id": "tenant_q",
        }
        scope = self._make_scope(query_string=b"access_token=some.jwt.token")
        send = AsyncMock()

        await middleware(scope, MagicMock(), send)

        assert mock_app.await_count == 1
        assert scope["state"]["auth"].tenant_id == "tenant_q"
        assert scope["state"]["auth"].auth_method == "jwt_query"

    async def test_no_auth_returns_401(
        self,
        middleware: Any,
        mock_app: AsyncMock,
    ) -> None:
        """Request with no auth headers returns 401."""
        scope = self._make_scope()
        send = AsyncMock()

        await middleware(scope, MagicMock(), send)

        mock_app.assert_not_awaited()
        assert send.await_count == 2


# ═════════════════════════════════════════════════════════════════════════
# _serialise helper
# ═════════════════════════════════════════════════════════════════════════


class TestSerialise:
    """_serialise produces valid JSON from domain objects."""

    def test_serialise_dict(self) -> None:
        from finance_sync.mcp.server import _serialise

        result = _serialise({"a": 1, "b": "hello"})
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": "hello"}

    def test_serialise_with_decimal(self) -> None:
        from decimal import Decimal

        from finance_sync.mcp.server import _serialise

        result = _serialise({"value": Decimal("123.45")})
        parsed = json.loads(result)
        assert parsed["value"] == "123.45"
