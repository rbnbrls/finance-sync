"""Contract tests + unit tests for the Plaid-like Open Banking connector.

Uses sandbox mode to test the connector without network calls.
"""

# pyright: basic

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from finance_sync.connectors.models import (
    CanonicalTransactionData,
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)

if TYPE_CHECKING:
    from finance_sync.connectors.plaid_like import PlaidLikeConnector


class TestPlaidLikeConnectorContract:
    """Contract tests for the Plaid-like Open Banking connector."""

    pytestmark = pytest.mark.asyncio

    # ── Fixtures ──────────────────────────────────────────────────────

    @pytest.fixture
    def plaid_config(self) -> ConnectorConfig:
        """Return a connector config with sandbox environment."""
        return ConnectorConfig(
            provider_type="plaid_like",
            credentials={
                "client_id": "test_client",
                "access_token": "access-sandbox-abc123",
            },
            options={
                "environment": "sandbox",
                "country_codes": ["NL", "US"],
            },
        )

    @pytest.fixture
    def plaid_connector(
        self, plaid_config: ConnectorConfig
    ) -> PlaidLikeConnector:
        """Return a PlaidLikeConnector with sandbox config."""
        from finance_sync.connectors.plaid_like import PlaidLikeConnector

        return PlaidLikeConnector(config=plaid_config)

    @pytest.fixture
    def sample_plaid_raw_data(
        self,
    ) -> tuple[list[RawAccount], list[RawTransaction]]:
        """Return sample data for transform tests."""
        return [
            RawAccount(
                external_account_id="plaid_acc_checking_01",
                name="Plaid Checking",
                account_type="depository",
                account_subtype="checking",
                currency_code="EUR",
                current_balance=Decimal("1250.50"),
                available_balance=Decimal("1200.00"),
                iso_currency_code="EUR",
            ),
            RawAccount(
                external_account_id="plaid_acc_credit_01",
                name="Plaid Credit Card",
                account_type="credit",
                account_subtype="credit card",
                currency_code="EUR",
                current_balance=Decimal("-450.25"),
                available_balance=Decimal("550.00"),
                iso_currency_code="EUR",
            ),
        ], [
            RawTransaction(
                external_transaction_id="plaid_tx_checking_001",
                external_account_id="plaid_acc_checking_01",
                amount=Decimal("-75.50"),
                currency_code="EUR",
                occurred_at=datetime(2025, 6, 15, tzinfo=UTC),
                description="Albert Heijn",
                transaction_type="payment",
                status="booked",
            ),
        ]

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """Connector should authenticate without raising in sandbox mode."""
        await plaid_connector.authenticate()

    async def test_authenticate_idempotent(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await plaid_connector.authenticate()
        await plaid_connector.authenticate()

    async def test_authenticate_missing_credentials(self) -> None:
        """Missing credentials in production mode should raise."""
        from finance_sync.connectors.plaid_like import PlaidLikeConnector

        config = ConnectorConfig(
            provider_type="plaid_like",
            credentials={},
            options={"environment": "production"},
        )
        conn = PlaidLikeConnector(config)
        with pytest.raises(Exception, match="client_id"):
            await conn.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await plaid_connector.health()
        assert health.provider_type == plaid_connector.name

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await plaid_connector.authenticate()
        accounts = await plaid_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) == 3  # 3 mock accounts

        checking = accounts[0]
        assert isinstance(checking, RawAccount)
        assert checking.external_account_id == "plaid_acc_checking_01"
        assert checking.account_type == "checking"
        assert checking.current_balance == Decimal("1250.50")

        savings = accounts[1]
        assert savings.account_type == "savings"

        credit = accounts[2]
        assert credit.account_type == "credit"

    async def test_fetch_accounts_idempotent(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await plaid_connector.authenticate()
        first = await plaid_connector.fetch_accounts()
        second = await plaid_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """fetch_transactions should return mock transactions in sandbox."""
        await plaid_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await plaid_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) >= 1

        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id
        assert txn.amount is not None

    async def test_fetch_transactions_with_account_filter(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """fetch_transactions should accept an account_id filter."""
        await plaid_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await plaid_connector.fetch_transactions(
            since=since, account_id="plaid_acc_checking_01"
        )
        assert isinstance(txns, list)

    async def test_fetch_transactions_with_limit(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await plaid_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await plaid_connector.fetch_transactions(since=since, limit=1)
        assert isinstance(txns, list)
        assert len(txns) <= 1

    async def test_production_fetch_uses_bounded_plaid_date_range(self) -> None:
        """Production remediation fetches Plaid transactions by date range."""
        from finance_sync.connectors.plaid_like import PlaidLikeConnector

        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/item/get":
                return httpx.Response(200, json={"item": {"item_id": "item-1"}})
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "transaction_id": "txn-1",
                            "account_id": "acct-1",
                            "amount": -12.5,
                            "iso_currency_code": "EUR",
                            "date": "2026-01-15",
                            "name": "Coffee",
                            "merchant_name": "Coffee Bar",
                            "pending": False,
                        }
                    ],
                    "total_transactions": 1,
                },
            )

        client = httpx.AsyncClient(
            base_url="https://plaid.test",
            transport=httpx.MockTransport(handler),
        )
        connector = PlaidLikeConnector(
            ConnectorConfig(
                provider_type="plaid_like",
                credentials={
                    "client_id": "client",
                    "secret": "secret",
                    "access_token": "access-production",
                },
                options={
                    "environment": "production",
                    "base_url": "https://plaid.test",
                },
            ),
            http_client=client,
        )

        await connector.authenticate()
        transactions = await connector.fetch_transactions(
            datetime(2026, 1, 1, tzinfo=UTC), account_id="acct-1", limit=1
        )
        await client.aclose()

        assert [request.url.path for request in requests] == [
            "/item/get",
            "/transactions/get",
        ]
        payload = json.loads(requests[1].content)
        assert payload["start_date"] == "2026-01-01"
        assert payload["account_id"] == "acct-1"
        assert payload["count"] == 1
        assert transactions[0].description == "Coffee Bar"

    async def test_production_rate_limit_is_typed(self) -> None:
        """Plaid 429 responses become retryable connector errors."""
        from finance_sync.connectors.exceptions import RateLimitError
        from finance_sync.connectors.plaid_like import PlaidLikeConnector

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "7"})

        client = httpx.AsyncClient(
            base_url="https://plaid.test",
            transport=httpx.MockTransport(handler),
        )
        connector = PlaidLikeConnector(
            ConnectorConfig(
                provider_type="plaid_like",
                credentials={
                    "client_id": "client",
                    "secret": "secret",
                    "access_token": "access-production",
                },
                options={
                    "environment": "production",
                    "base_url": "https://plaid.test",
                },
            ),
            http_client=client,
        )

        with pytest.raises(RateLimitError):
            await connector.authenticate()
        await client.aclose()

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_with_normalisation(
        self,
        plaid_connector: PlaidLikeConnector,
        sample_plaid_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should normalise Plaid account types."""
        raw_accounts, _ = sample_plaid_raw_data
        canonical = plaid_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)

        # First account: depository/checking → checking
        checking = canonical[0]
        assert checking.account_type == "checking"
        assert checking.external_account_id == "plaid_acc_checking_01"

        # Second account: credit → credit
        credit = canonical[1]
        assert credit.account_type == "credit"

    async def test_transform_transactions_roundtrip(
        self,
        plaid_connector: PlaidLikeConnector,
        sample_plaid_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_plaid_raw_data
        canonical = plaid_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "plaid_like"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(plaid_connector.name, str)
        assert plaid_connector.name == "plaid_like"
        assert plaid_connector.name == plaid_connector.config.provider_type

    async def test_display_name(
        self, plaid_connector: PlaidLikeConnector
    ) -> None:
        """display_name should be set."""
        assert plaid_connector.display_name == "Plaid-like Open Banking"
