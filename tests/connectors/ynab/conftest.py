"""pytest configuration for YNAB connector tests.

Provides a mock HTTP transport that intercepts calls to the YNAB API
and returns canned responses from :mod:`ynab_api_fixtures`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.ynab import YnabConnector
from tests.connectors.fixtures.ynab_api_fixtures import (
    BUDGET_ACCOUNTS_RESPONSE,
    BUDGET_TRANSACTIONS_RESPONSE,
    BUDGETS_RESPONSE,
)


class YnabApiMockTransport(httpx.MockTransport):
    """Mock transport that returns canned YNAB API responses.

    Routes are matched by URL path.  Unmatched paths raise
    404 so tests fail loudly on unexpected endpoints.
    """

    def __init__(self) -> None:
        super().__init__(self._handler)
        self._call_log: list[dict[str, object]] = []

    @property
    def call_log(self) -> list[dict[str, object]]:
        """List of all requests made through this transport."""
        return list(self._call_log)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self._call_log.append(
            {"method": request.method, "url": str(request.url)}
        )

        path = request.url.path

        # GET /budgets
        if request.method == "GET" and path == "/v1/budgets":
            return httpx.Response(200, json=BUDGETS_RESPONSE)

        # GET /budgets/{id}/accounts
        if (
            request.method == "GET"
            and "/budgets/" in path
            and path.endswith("/accounts")
        ):
            return httpx.Response(200, json=BUDGET_ACCOUNTS_RESPONSE)

        # GET /budgets/{id}/transactions
        # or /budgets/{id}/accounts/{account_id}/transactions
        if request.method == "GET" and "/transactions" in path:
            return httpx.Response(200, json=BUDGET_TRANSACTIONS_RESPONSE)

        msg = f"No mock handler for {request.method} {path}"
        return httpx.Response(404, json={"error": msg})


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def ynab_connector_config() -> ConnectorConfig:
    """Return a YNAB ConnectorConfig with test credentials."""
    return ConnectorConfig(
        provider_type="ynab",
        credentials={"access_token": "test_ynab_token_abc123"},
        options={"budget_id": "ynab_budget_001"},
    )


@pytest.fixture
def ynab_mock_transport() -> YnabApiMockTransport:
    """Return a fresh mock transport for the YNAB API."""
    return YnabApiMockTransport()


@pytest.fixture
def ynab_connector(
    ynab_connector_config: ConnectorConfig,
    ynab_mock_transport: YnabApiMockTransport,
) -> YnabConnector:
    """Return a YnabConnector with mock HTTP transport."""
    http_client = httpx.AsyncClient(
        base_url="https://api.youneedabudget.com/v1",
        transport=ynab_mock_transport,
    )
    return YnabConnector(
        config=ynab_connector_config,
        http_client=http_client,
    )


@pytest.fixture
def sample_ynab_raw_data() -> tuple[list[Any], list[Any]]:
    """Return sample raw accounts and transactions for transform tests."""
    from finance_sync.connectors.ynab import YnabConnector
    from tests.connectors.fixtures.ynab_api_fixtures import (
        BUDGET_ACCOUNTS_RESPONSE,
        BUDGET_TRANSACTIONS_RESPONSE,
    )

    connector = YnabConnector(
        config=ConnectorConfig(
            provider_type="ynab",
            credentials={"access_token": "test"},
        ),
    )

    accounts_data = BUDGET_ACCOUNTS_RESPONSE.get("data", {}).get("accounts", [])
    raw_accounts = [
        connector._parse_account(a, "ynab_budget_001") for a in accounts_data
    ]

    txns_data = BUDGET_TRANSACTIONS_RESPONSE.get("data", {}).get(
        "transactions", []
    )
    raw_txns = [
        connector._parse_transaction(t, "ynab_budget_001")
        for t in txns_data
        if not t.get("transfer_account_id")  # skip transfer for cleaner test
    ]

    return raw_accounts, raw_txns
