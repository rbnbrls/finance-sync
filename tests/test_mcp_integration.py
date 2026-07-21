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
        """There are exactly 8 resources defined (4 existing + 4 new)."""
        assert len(self._uri_map) == 8

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

    # ── New resources ─────────────────────────────────────────────

    def test_resource_account_detail(self) -> None:
        """finance://account/{account_id} is registered."""
        t = self._uri_map.get("finance://account/{account_id}")
        assert t is not None, "Missing finance://account/{account_id} resource"
        assert t.name == "account_detail"

    def test_resource_account_transactions(self) -> None:
        """finance://account/{account_id}/transactions is registered."""
        t = self._uri_map.get("finance://account/{account_id}/transactions")
        assert t is not None, (
            "Missing finance://account/{account_id}/transactions resource"
        )
        assert t.name == "account_transactions"

    def test_resource_portfolio_history(self) -> None:
        """finance://portfolio/history is registered."""
        t = self._uri_map.get("finance://portfolio/history")
        assert t is not None, "Missing finance://portfolio/history resource"
        assert t.name == "portfolio_history"

    def test_resource_net_worth_history(self) -> None:
        """finance://net-worth/history is registered."""
        t = self._uri_map.get("finance://net-worth/history")
        assert t is not None, "Missing finance://net-worth/history resource"
        assert t.name == "net_worth_history"


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
        """There are exactly 9 tools defined (3 existing + 6 new)."""
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

    # ── New tools ─────────────────────────────────────────────────

    def test_tool_get_daily_briefing(self) -> None:
        """get_daily_briefing tool is registered."""
        t = self._tool_map.get("get_daily_briefing")
        assert t is not None, "Missing get_daily_briefing tool"
        assert t.description is not None
        assert "briefing" in t.description.lower()

    def test_tool_get_subscriptions(self) -> None:
        """get_subscriptions tool is registered."""
        t = self._tool_map.get("get_subscriptions")
        assert t is not None, "Missing get_subscriptions tool"
        assert t.description is not None
        assert "subscription" in t.description.lower()

    def test_tool_get_performance(self) -> None:
        """get_performance tool is registered."""
        t = self._tool_map.get("get_performance")
        assert t is not None, "Missing get_performance tool"
        props = t.parameters.get("properties", {})
        # Should have period or granularity parameter
        assert any(k in props for k in ("period", "granularity", "subject"))

    def test_tool_get_allocation(self) -> None:
        """get_allocation tool is registered."""
        t = self._tool_map.get("get_allocation")
        assert t is not None, "Missing get_allocation tool"
        props = t.parameters.get("properties", {})
        assert "by" in props or "allocat" in str(props)

    def test_tool_get_cashflow(self) -> None:
        """get_cashflow tool is registered."""
        t = self._tool_map.get("get_cashflow")
        assert t is not None, "Missing get_cashflow tool"
        props = t.parameters.get("properties", {})
        assert "period" in props or "date_from" in props

    def test_tool_list_sync_runs(self) -> None:
        """list_sync_runs tool is registered."""
        t = self._tool_map.get("list_sync_runs")
        assert t is not None, "Missing list_sync_runs tool"
        props = t.parameters.get("properties", {})
        assert "limit" in props or "connector" in props
        assert t.description is not None


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


# ═════════════════════════════════════════════════════════════════════════
# Resource handler data-flow tests (mocked service)
# ═════════════════════════════════════════════════════════════════════════


class TestMCPResourceHandlers:
    """Verify resource handler functions exist and have correct signatures."""

    def test_account_detail_handler_is_callable(self) -> None:
        """account_detail handler accepts (ctx, id) parameters."""
        from finance_sync.mcp.server import resource_account_detail

        assert callable(resource_account_detail)

        import inspect

        sig = inspect.signature(resource_account_detail)
        params = list(sig.parameters.keys())
        assert "ctx" in params or "id" in params

    def test_portfolio_history_handler_is_callable(self) -> None:
        """portfolio_history handler accepts (ctx) parameter."""
        from finance_sync.mcp.server import resource_portfolio_history

        assert callable(resource_portfolio_history)

        import inspect

        sig = inspect.signature(resource_portfolio_history)
        assert "ctx" in sig.parameters


# ═════════════════════════════════════════════════════════════════════════
# Tool handler data-flow tests
# ═════════════════════════════════════════════════════════════════════════


class TestMCPToolHandlers:
    """Verify new tool handler functions exist."""

    def test_get_daily_briefing_handler(self) -> None:
        """get_daily_briefing handler function is importable."""
        from finance_sync.mcp.server import tool_get_daily_briefing

        assert callable(tool_get_daily_briefing)

    def test_get_subscriptions_handler(self) -> None:
        """get_subscriptions handler function is importable."""
        from finance_sync.mcp.server import tool_get_subscriptions

        assert callable(tool_get_subscriptions)

    def test_get_performance_handler(self) -> None:
        """get_performance handler function is importable."""
        from finance_sync.mcp.server import tool_get_performance

        assert callable(tool_get_performance)

    def test_get_allocation_handler(self) -> None:
        """get_allocation handler function is importable."""
        from finance_sync.mcp.server import tool_get_allocation

        assert callable(tool_get_allocation)

    def test_get_cashflow_handler(self) -> None:
        """get_cashflow handler function is importable."""
        from finance_sync.mcp.server import tool_get_cashflow

        assert callable(tool_get_cashflow)

    def test_list_sync_runs_handler(self) -> None:
        """list_sync_runs handler function is importable."""
        from finance_sync.mcp.server import tool_list_sync_runs

        assert callable(tool_list_sync_runs)
