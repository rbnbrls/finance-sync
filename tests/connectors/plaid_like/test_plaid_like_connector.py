"""Contract tests + unit tests for the Plaid-like Open Banking connector.

Uses sandbox mode to test the connector without network calls.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

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
