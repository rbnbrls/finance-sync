"""pytest configuration for Trading212 connector tests.

Provides a mock HTTP transport that intercepts calls to the Trading212
API and returns canned responses from :mod:`trading212_api_fixtures`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.trading212 import (
    Trading212Connector,
    _parse_cash_transaction,
    _parse_order,
)
from tests.connectors.fixtures.trading212_api_fixtures import (
    ACCOUNT_CASH_RESPONSE,
    ACCOUNT_INFO_RESPONSE,
    DIVIDEND_AAPL,
    ORDER_BUY_AAPL,
    ORDER_HISTORY_RESPONSE,
    PORTFOLIO_RESPONSE,
    TRANSACTION_HISTORY_RESPONSE,
)


class Trading212MockTransport(httpx.MockTransport):
    """Mock transport that returns canned Trading212 API responses.

    Routes are matched by URL path.  Unmatched paths raise
    ``httpx.HTTPStatusError(404)`` so tests fail loudly if they
    hit unexpected endpoints.
    """

    def __init__(self) -> None:
        super().__init__(self._handler)
        self._call_log: list[dict[str, object]] = []

    @property
    def call_log(self) -> list[dict[str, object]]:
        """List of all requests made through this transport, for assertions."""
        return list(self._call_log)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self._call_log.append(
            {"method": request.method, "url": str(request.url)}
        )

        path = request.url.path

        # GET /api/v0/equity/account/cash
        if request.method == "GET" and path == "/api/v0/equity/account/cash":
            return httpx.Response(200, json=ACCOUNT_CASH_RESPONSE)

        # GET /api/v0/equity/account/info
        if request.method == "GET" and path == "/api/v0/equity/account/info":
            return httpx.Response(200, json=ACCOUNT_INFO_RESPONSE)

        # GET /api/v0/equity/portfolio
        if request.method == "GET" and path == "/api/v0/equity/portfolio":
            return httpx.Response(200, json=PORTFOLIO_RESPONSE)

        # GET /api/v0/equity/history/orders
        if request.method == "GET" and "/history/orders" in path:
            return self._handle_orders(str(request.url))

        # GET /api/v0/equity/history/transactions
        if request.method == "GET" and "/history/transactions" in path:
            return self._handle_transactions(str(request.url))

        msg = f"No mock handler for {request.method} {path}"
        return httpx.Response(404, json={"error": msg})

    def _handle_orders(self, url: str) -> httpx.Response:
        """Return order history response with optional pagination."""
        if "cursor=page2_cursor" in url:
            # Lazy import to avoid circulars
            from tests.connectors.fixtures.trading212_api_fixtures import (
                ORDER_HISTORY_PAGE_2,
            )

            return httpx.Response(200, json=ORDER_HISTORY_PAGE_2)
        return httpx.Response(200, json=ORDER_HISTORY_RESPONSE)

    def _handle_transactions(self, url: str) -> httpx.Response:
        """Return transaction history response with optional pagination."""
        if "cursor=txn_page2" in url:
            from tests.connectors.fixtures.trading212_api_fixtures import (
                TRANSACTION_HISTORY_PAGE_2,
            )

            return httpx.Response(200, json=TRANSACTION_HISTORY_PAGE_2)
        return httpx.Response(200, json=TRANSACTION_HISTORY_RESPONSE)


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def t212_connector_config() -> ConnectorConfig:
    """Return a Trading212 ``ConnectorConfig`` with test credentials."""
    return ConnectorConfig(
        provider_type="trading212",
        credentials={"api_key": "test_t212_api_key_abc123"},
        options={"demo": False},
    )


@pytest.fixture
def t212_mock_transport() -> Trading212MockTransport:
    """Return a fresh mock transport for the Trading212 API."""
    return Trading212MockTransport()


@pytest.fixture
def t212_connector(
    t212_connector_config: ConnectorConfig,
    t212_mock_transport: Trading212MockTransport,
) -> Trading212Connector:
    """Return a ``Trading212Connector`` with mock HTTP transport."""
    http_client = httpx.AsyncClient(
        base_url="https://live.trading212.com",
        transport=t212_mock_transport,
    )
    return Trading212Connector(
        config=t212_connector_config,
        http_client=http_client,
    )


@pytest.fixture
def sample_t212_raw_data() -> tuple[list[Any], list[Any]]:
    """Return sample raw accounts and transactions for transform tests."""
    _accounts: list[Any] = []
    # Parse one order and one dividend transaction
    order_txn = _parse_order(ORDER_BUY_AAPL, "12345678")
    div_txn = _parse_cash_transaction(DIVIDEND_AAPL, "12345678")
    txns = [order_txn, div_txn]
    return _accounts, txns
