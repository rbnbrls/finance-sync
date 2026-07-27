"""Integration tests for the finance-sync MCP server.

Covers resource/tool registration and the helpers that wire domain
services.  Auth, middleware, and context-var behaviour are already
tested in ``test_mcp_server.py``.
"""

from __future__ import annotations

from datetime import UTC

import pytest

# ═════════════════════════════════════════════════════════════════════════
# Resource registration tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPResourcesCompleteness:
    """Verify ALL expected resources are registered."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        from finance_sync.mcp.server import mcp

        self._mcp = mcp
        self._templates = mcp._resource_manager.list_templates()
        self._uri_map = {str(t.uri_template): t for t in self._templates}

    def test_resource_count(self) -> None:
        """There are exactly 4 resources defined."""
        assert len(self._uri_map) == 4

    # ── Existing resources (regression) ────────────────────────────

    def test_resource_accounts(self) -> None:
        """finance://accounts is registered."""
        t = self._uri_map.get("finance://accounts")
        assert t is not None
        assert t.name == "accounts"
        assert t.title == "Account List"

    def test_resource_portfolio(self) -> None:
        """finance://portfolio is registered."""
        t = self._uri_map.get("finance://portfolio")
        assert t is not None
        assert t.name == "portfolio"

    def test_resource_transactions(self) -> None:
        """finance://transactions is registered."""
        t = self._uri_map.get("finance://transactions")
        assert t is not None
        assert t.name == "transactions"

    def test_resource_net_worth(self) -> None:
        """finance://net-worth is registered."""
        t = self._uri_map.get("finance://net-worth")
        assert t is not None
        assert t.name == "net_worth"


# ═════════════════════════════════════════════════════════════════════════
# Tool registration tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPToolsCompleteness:
    """Verify ALL expected tools are registered."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        from finance_sync.mcp.server import mcp

        self._mcp = mcp
        self._tools = mcp._tool_manager.list_tools()
        self._tool_map = {t.name: t for t in self._tools}

    def test_tool_count(self) -> None:
        """There are exactly 9 tools defined."""
        assert len(self._tool_map) == 9

    # ── Existing tools (regression) ────────────────────────────────

    def test_tool_run_sync(self) -> None:
        """run_sync tool is registered."""
        t = self._tool_map.get("run_sync")
        assert t is not None
        assert t.parameters is not None
        props = t.parameters.get("properties", {})
        assert "connector_type" in props

    def test_tool_get_summary(self) -> None:
        """get_summary tool is registered."""
        t = self._tool_map.get("get_summary")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "timeframe" in props

    def test_tool_resolve_security(self) -> None:
        """resolve_security tool is registered."""
        t = self._tool_map.get("resolve_security")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "query" in props

    # ── New tools (performance-analytics) ───────────────────────────

    def test_tool_get_performance(self) -> None:
        """get_performance tool is registered."""
        t = self._tool_map.get("get_performance")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "period" in props

    def test_tool_get_allocation(self) -> None:
        """get_allocation tool is registered."""
        t = self._tool_map.get("get_allocation")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "by" in props

    def test_tool_get_cashflow(self) -> None:
        """get_cashflow tool is registered."""
        t = self._tool_map.get("get_cashflow")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "period" in props

    def test_tool_list_sync_runs(self) -> None:
        """list_sync_runs tool is registered."""
        t = self._tool_map.get("list_sync_runs")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "limit" in props

    def test_tool_get_subscriptions(self) -> None:
        """get_subscriptions tool is registered."""
        t = self._tool_map.get("get_subscriptions")
        assert t is not None
        props = t.parameters.get("properties", {})
        assert "active_only" in props


# ═════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPServerHelpers:
    """Verify server helper functions work correctly."""

    def test_serialise_with_datetime(self) -> None:
        """_serialise handles datetime objects."""
        from datetime import datetime

        from finance_sync.mcp.server import _serialise

        result = _serialise({"ts": datetime(2025, 1, 1, tzinfo=UTC)})
        assert "2025" in result
        assert result.startswith("{")

    def test_serialise_with_nested_models(self) -> None:
        """_serialise handles nested pydantic models via model_dump."""
        from finance_sync.mcp.server import _serialise
        from finance_sync.services.read_api import AccountSummary

        acct = AccountSummary(
            id="abc-123",
            name="Test",
            account_type="checking",
            currency_code="EUR",
            is_active=True,
            provider_key="test",
        )
        result = _serialise({"account": acct.model_dump()})
        assert "abc-123" in result
        assert "Test" in result


# ═════════════════════════════════════════════════════════════════════════
# Server instantiation tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPServerInstantiation:
    """Verify the MCP server can be instantiated and the ASGI app builds."""

    def test_sse_app_factory(self) -> None:
        """create_sse_app returns an ASGI app with auth middleware."""
        from finance_sync.mcp.server import create_sse_app

        instance = create_sse_app()
        assert instance is not None
        # Should be wrapped with auth middleware
        assert "auth" in type(instance).__name__.lower()

    def test_module_app_is_sse_app(self) -> None:
        """Module-level `app` is the result of create_sse_app()."""
        from finance_sync.mcp.server import app

        actual = type(app).__name__
        # App should be wrapped in auth middleware
        assert "auth" in actual.lower()

    def test_mcp_settings(self) -> None:
        """FastMCP has expected host, port, and transport config."""
        from finance_sync.mcp.server import mcp

        assert mcp.settings.host == "0.0.0.0"
        assert mcp.settings.port == 8100


# ═════════════════════════════════════════════════════════════════════════
# __main__ module tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPServerMain:
    """Verify __main__ can be invoked (import-level)."""

    def test_main_import(self) -> None:
        """__main__ imports without error."""
        from finance_sync.mcp import __main__ as mod

        assert mod is not None
        assert callable(mod.main)
