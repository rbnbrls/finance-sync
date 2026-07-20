"""Tests for the Read API endpoints Phase 4.1.

# pyright: basic

Tests cover:
- API endpoint registration (OpenAPI schema)
- Authentication guards on all new endpoints
- Rate limiting middleware
- ReadService unit tests (mocked session)
- Portfolio calculations
- Net worth aggregation
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.services.read_api import ReadService

# ── Test helpers ──────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"


def _assert_sql_contains(mock: AsyncMock, fragment: str) -> None:
    """Assert that the compiled SQL from execute() contains ``fragment``."""
    call_args = mock.execute.call_args
    assert call_args is not None
    stmt = str(
        call_args[0][0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert fragment in stmt


# ── Shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        access_token_expire_minutes=15,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def svc(mock_session: AsyncMock) -> ReadService:
    return ReadService(mock_session)


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI schema — all endpoints registered
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """Verify all Phase 4.1 endpoints appear in the OpenAPI schema."""

    def test_accounts_endpoints_registered(self, client: TestClient) -> None:
        paths: dict[str, Any] = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/accounts" in paths
        assert paths["/api/v1/accounts"]["get"]["tags"] == ["accounts"]
        assert "/api/v1/accounts/{account_id}" in paths
        assert "/api/v1/accounts/{account_id}/transactions" in paths
        assert "/api/v1/accounts/{account_id}/balances" in paths

    def test_portfolio_endpoints_registered(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/portfolio" in paths
        assert paths["/api/v1/portfolio"]["get"]["tags"] == ["portfolio"]
        assert "/api/v1/portfolio/history" in paths

    def test_net_worth_endpoints_registered(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/net-worth" in paths
        assert paths["/api/v1/net-worth"]["get"]["tags"] == ["net-worth"]
        assert "/api/v1/net-worth/history" in paths

    def test_sync_runs_endpoint_registered(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/sync-runs" in paths
        assert paths["/api/v1/sync-runs"]["get"]["tags"] == ["sync-runs"]

    def test_securities_list_and_prices_registered(
        self, client: TestClient
    ) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        # List canonical securities
        assert "/api/v1/securities" in paths
        get_ops = paths["/api/v1/securities"]["get"]
        params = {p["name"] for p in get_ops.get("parameters", [])}
        assert "search" in params
        assert "security_type" in params

        # Price time series
        assert "/api/v1/securities/{security_id}/prices" in paths
        price_params = {
            p["name"]
            for p in paths["/api/v1/securities/{security_id}/prices"][
                "get"
            ].get("parameters", [])
        }
        assert "interval" in price_params
        assert "date_from" in price_params
        assert "date_to" in price_params


# ═══════════════════════════════════════════════════════════════════════
# Authentication guards
# ═══════════════════════════════════════════════════════════════════════


class TestAuthGuards:
    """All new read endpoints require authentication."""

    ENDPOINTS = [
        ("GET", "/api/v1/accounts"),
        ("GET", "/api/v1/accounts/fake-id"),
        ("GET", "/api/v1/accounts/fake-id/transactions"),
        ("GET", "/api/v1/accounts/fake-id/balances"),
        ("GET", "/api/v1/portfolio"),
        ("GET", "/api/v1/portfolio/history"),
        ("GET", "/api/v1/net-worth"),
        ("GET", "/api/v1/net-worth/history"),
        ("GET", "/api/v1/sync-runs"),
        ("GET", "/api/v1/securities"),
        ("GET", "/api/v1/securities/fake-id/prices"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_unauthenticated_returns_401(
        self, client: TestClient, method: str, path: str
    ) -> None:
        response: Response = client.request(method, path)
        assert response.status_code == 401
        assert "detail" in response.json()

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_bad_token_returns_401(
        self, client: TestClient, method: str, path: str
    ) -> None:
        headers = {"Authorization": "Bearer invalid-token-here"}
        response: Response = client.request(method, path, headers=headers)
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# Rate limiting middleware
# ═══════════════════════════════════════════════════════════════════════


class TestRateLimitMiddleware:
    """Verify rate limiting headers and 429 behaviour."""

    def test_rate_limit_headers_present(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/")
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_exempt_paths_not_rate_limited(self, client: TestClient) -> None:
        """Health endpoint should not have rate-limit headers."""
        response: Response = client.get("/health")
        assert "X-RateLimit-Limit" not in response.headers

    def test_normal_requests_succeed(self, client: TestClient) -> None:
        """A few requests should not trigger rate limiting."""
        key = "Bearer test-rate-normal"
        for _ in range(5):
            resp: Response = client.get(
                "/api/v1/", headers={"Authorization": key}
            )
            # 200 means OK, 401 is also OK (bad token but not rate-limited)
            assert resp.status_code in (200, 401)


# ═══════════════════════════════════════════════════════════════════════
# ReadService unit tests (mocked session)
# ═══════════════════════════════════════════════════════════════════════


class TestReadServiceListAccounts:
    """ReadService.list_accounts() behaviour."""

    async def test_empty_tenant(self, svc: ReadService) -> None:
        result = await svc.list_accounts(tenant_id="t1")
        assert result.total == 0
        assert result.items == []

    async def test_passes_tenant_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_accounts(tenant_id="tenant-abc")
        _assert_sql_contains(mock_session, "tenant-abc")


class TestReadServiceListTransactions:
    """ReadService.list_account_transactions() behaviour."""

    async def test_filters_by_account(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_account_transactions(tenant_id="t1", account_id="acct-1")
        _assert_sql_contains(mock_session, "acct-1")

    async def test_date_range_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        since = datetime(2025, 1, 1, tzinfo=UTC)
        until = datetime(2025, 6, 30, tzinfo=UTC)
        await svc.list_account_transactions(
            tenant_id="t1",
            account_id="acct-1",
            date_from=since,
            date_to=until,
        )
        _assert_sql_contains(mock_session, "2025-01-01")
        _assert_sql_contains(mock_session, "2025-06-30")


class TestReadServiceListBalances:
    """ReadService.list_account_balances() behaviour."""

    async def test_balance_kind_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_account_balances(
            tenant_id="t1",
            account_id="acct-1",
            balance_kind="available",
        )
        _assert_sql_contains(mock_session, "available")


class TestReadServiceListSecurities:
    """ReadService.list_securities() behaviour."""

    async def test_empty(self, svc: ReadService) -> None:
        result = await svc.list_securities()
        assert result.total == 0
        assert result.items == []

    async def test_type_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_securities(security_type="stock")
        _assert_sql_contains(mock_session, "stock")

    async def test_search_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_securities(search="AAPL")
        _assert_sql_contains(mock_session, "AAPL")


class TestReadServiceSecurityPrices:
    """ReadService.get_security_prices() behaviour."""

    async def test_default_interval(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.get_security_prices(security_id="sec-1")
        _assert_sql_contains(mock_session, "sec-1")

    async def test_date_range(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        since = datetime(2025, 1, 1, tzinfo=UTC)
        await svc.get_security_prices(
            security_id="sec-1", date_from=since, interval="1h"
        )
        # Should have at least one execute call
        assert mock_session.execute.call_count >= 1


class TestReadServicePortfolio:
    """ReadService.get_portfolio() behaviour."""

    async def test_empty_returns_zero(self, svc: ReadService) -> None:
        result = await svc.get_portfolio(tenant_id="t1")
        assert result.total_value == Decimal(0)
        assert result.accounts == []


class TestReadServiceSyncRuns:
    """ReadService.list_sync_runs() behaviour."""

    async def test_empty(self, svc: ReadService) -> None:
        result = await svc.list_sync_runs()
        assert result.total == 0
        assert result.items == []
        assert result.status_counts == []

    async def test_connector_filter(self, mock_session: AsyncMock) -> None:
        svc = ReadService(mock_session)
        await svc.list_sync_runs(connector="bunq")
        _assert_sql_contains(mock_session, "bunq")


class TestReadServiceNetWorth:
    """ReadService.get_net_worth() behaviour."""

    async def test_empty_tenant(self, svc: ReadService) -> None:
        result = await svc.get_net_worth(tenant_id="t1")
        assert result.net_worth == Decimal(0)
        assert result.total_assets == Decimal(0)
        assert result.accounts == []


# ═══════════════════════════════════════════════════════════════════════
# Sliding-window rate limiter unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestSlidingWindowEntry:
    """SlidingWindowEntry behaviour."""

    def test_is_allowed_within_limit(self) -> None:
        from finance_sync.api.middleware.rate_limit import SlidingWindowEntry

        entry = SlidingWindowEntry(max_requests=3, window_seconds=60.0)
        assert entry.is_allowed() is True
        assert entry.is_allowed() is True
        assert entry.is_allowed() is True
        assert entry.is_allowed() is False  # exceeded

    def test_reset_clears(self) -> None:
        from finance_sync.api.middleware.rate_limit import SlidingWindowEntry

        entry = SlidingWindowEntry(max_requests=1, window_seconds=60.0)
        assert entry.is_allowed() is True
        assert entry.is_allowed() is False
        entry.reset()
        assert entry.is_allowed() is True
