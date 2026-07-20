"""pytest configuration for bunq connector tests.

Provides a mock HTTP transport that intercepts calls to the bunq API
and returns canned responses from :mod:`bunq_api_fixtures`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from finance_sync.connectors.bunq import BunqConnector
from finance_sync.connectors.models import ConnectorConfig
from tests.connectors.fixtures.bunq_api_fixtures import (
    MONETARY_ACCOUNTS_RESPONSE,
    PAGE_1_RESPONSE,
    PAGE_2_RESPONSE,
    PAYMENTS_ACCOUNT_1000001,
    PAYMENTS_ACCOUNT_1000002,
    PAYMENTS_ACCOUNT_1000003,
    SESSION_SERVER_RESPONSE,
)


class BunqApiMockTransport(httpx.MockTransport):
    """Mock transport that returns canned bunq API responses.

    Routes are matched by URL path.  Unmatched paths raise
    ``httpx.HTTPStatusError(404)`` so tests fail loudly if they
    hit unexpected endpoints.
    """

    def __init__(self) -> None:
        super().__init__(self._handler)
        self._call_log: list[dict[str, object]] = []
        self._paginators: dict[str, int] = {}

    @property
    def call_log(self) -> list[dict[str, object]]:
        """List of all requests made through this transport, for assertions."""
        return list(self._call_log)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self._call_log.append(
            {"method": request.method, "url": str(request.url)}
        )

        path = request.url.path

        # POST /v1/session-server
        if request.method == "POST" and path == "/v1/session-server":
            return httpx.Response(200, json=SESSION_SERVER_RESPONSE)

        # GET /v1/monetary-account/<id>/payment
        # Must be before monetary-account since payment URLs also match.
        if request.method == "GET" and "/payment" in path:
            return self._handle_payments(str(request.url))

        # GET /v1/user/<id>/monetary-account
        if request.method == "GET" and "monetary-account" in path:
            # Check if it's a paginated request
            if "newer_id" in str(request.url):
                return self._handle_paginated_accounts(str(request.url))
            return httpx.Response(200, json=MONETARY_ACCOUNTS_RESPONSE)

        msg = f"No mock handler for {request.method} {path}"
        return httpx.Response(404, json={"error": msg})

    def _handle_paginated_accounts(
        self,
        url: str,
    ) -> httpx.Response:
        """Return page 1 or page 2 based on query params."""
        if "newer_id=1000001" in url:
            return httpx.Response(200, json=PAGE_2_RESPONSE)
        return httpx.Response(200, json=PAGE_1_RESPONSE)

    def _handle_payments(
        self,
        url: str,
    ) -> httpx.Response:
        """Return payments for the account ID in the URL path."""
        if "monetary-account/1000001/payment" in url:
            return httpx.Response(200, json=PAYMENTS_ACCOUNT_1000001)
        if "monetary-account/1000002/payment" in url:
            return httpx.Response(200, json=PAYMENTS_ACCOUNT_1000002)
        if "monetary-account/1000003/payment" in url:
            return httpx.Response(200, json=PAYMENTS_ACCOUNT_1000003)
        return httpx.Response(200, json={"Response": [], "Pagination": {}})


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def bunq_connector_config() -> ConnectorConfig:
    """Return a bunq ``ConnectorConfig`` with test credentials."""
    return ConnectorConfig(
        provider_type="bunq",
        credentials={"api_key": "test_bunq_api_key_abc123"},
        options={"sandbox": True},
    )


@pytest.fixture
def bunq_mock_transport() -> BunqApiMockTransport:
    """Return a fresh mock transport for the bunq API."""
    return BunqApiMockTransport()


@pytest.fixture
def bunq_connector(
    bunq_connector_config: ConnectorConfig,
    bunq_mock_transport: BunqApiMockTransport,
) -> BunqConnector:
    """Return a ``BunqConnector`` with mock HTTP transport."""
    http_client = httpx.AsyncClient(
        base_url="https://api.bunq.com/v1",
        transport=bunq_mock_transport,
    )
    return BunqConnector(
        config=bunq_connector_config,
        http_client=http_client,
    )


@pytest.fixture
def sample_bunq_raw_data() -> tuple[list[Any], list[Any]]:
    """Return sample raw accounts and transactions for transform tests."""
    from finance_sync.connectors.bunq import BunqConnector
    from tests.connectors.fixtures.bunq_api_fixtures import (
        MONETARY_ACCOUNT_BANK,
        MONETARY_ACCOUNT_SAVINGS,
        PAYMENT_PURCHASE,
        SAVINGS_GOAL_ACCOUNT,
    )

    # Parse the fixture JSON through the connector's parser
    bank = BunqConnector._parse_account(
        MONETARY_ACCOUNT_BANK["MonetaryAccountBank"], "MonetaryAccountBank"
    )
    savings = BunqConnector._parse_account(
        MONETARY_ACCOUNT_SAVINGS["MonetaryAccountSavings"],
        "MonetaryAccountSavings",
    )
    goal = BunqConnector._parse_account(
        SAVINGS_GOAL_ACCOUNT["MonetaryAccountSavings"],
        "MonetaryAccountSavings",
    )
    txn = BunqConnector._parse_payment(PAYMENT_PURCHASE["Payment"], "1000001")

    return [bank, savings, goal], [txn]
