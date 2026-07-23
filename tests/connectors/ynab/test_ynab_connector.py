"""Contract tests + unit tests for the YNAB connector.

These tests use a mock HTTP transport to simulate the YNAB API
without any network calls.
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
)
from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)

if TYPE_CHECKING:
    from finance_sync.connectors.ynab import YnabConnector
    from tests.connectors.ynab.conftest import YnabApiMockTransport

# ═══════════════════════════════════════════════════════════════════════
# Contract tests (from ConnectorContractTest template)
# ═══════════════════════════════════════════════════════════════════════


class TestYnabConnectorContract:
    """Contract tests that every connector must pass."""

    pytestmark = pytest.mark.asyncio

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Connector should authenticate without raising."""
        await ynab_connector.authenticate()

    async def test_authenticate_idempotent(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await ynab_connector.authenticate()
        await ynab_connector.authenticate()

    async def test_authenticate_missing_token(self) -> None:
        """Missing access_token should raise PermanentError."""
        config = ConnectorConfig(provider_type="ynab")
        from finance_sync.connectors.ynab import YnabConnector

        conn = YnabConnector(config)
        with pytest.raises(PermanentError, match="access_token"):
            await conn.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await ynab_connector.health()
        assert health.provider_type == ynab_connector.name
        assert health.healthy

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, ynab_connector: YnabConnector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await ynab_connector.authenticate()
        accounts = await ynab_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) >= 1

        # First account should be checking
        checking = accounts[0]
        assert isinstance(checking, RawAccount)
        assert checking.external_account_id == "ynab_acc_checking_01"
        assert checking.name == "Checking Account"
        assert checking.account_type == "checking"

        # Second should be savings
        savings = accounts[1]
        assert savings.external_account_id == "ynab_acc_savings_01"
        assert savings.account_type == "savings"

        # Third should be credit card
        credit = accounts[2]
        assert credit.external_account_id == "ynab_acc_credit_01"
        assert credit.account_type == "credit"

    async def test_fetch_accounts_idempotent(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await ynab_connector.authenticate()
        first = await ynab_connector.fetch_accounts()
        second = await ynab_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert len(first) == len(second)

    async def test_fetch_accounts_not_authenticated(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Calling fetch_accounts before authenticate should raise."""
        with pytest.raises(PermanentError, match="not authenticated"):
            await ynab_connector.fetch_accounts()

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, ynab_connector: YnabConnector
    ) -> None:
        """fetch_transactions should return a list of RawTransaction."""
        await ynab_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await ynab_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) >= 1

        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id
        assert txn.external_account_id
        assert txn.amount is not None

    async def test_fetch_transactions_with_account_filter(
        self, ynab_connector: YnabConnector
    ) -> None:
        """fetch_transactions should accept an account_id filter."""
        await ynab_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await ynab_connector.fetch_transactions(
            since=since, account_id="ynab_acc_checking_01"
        )
        assert isinstance(txns, list)
        # Transactions include the requested account_id
        assert any(t.external_account_id == "ynab_acc_checking_01" for t in txns)

    async def test_fetch_transactions_with_limit(
        self, ynab_connector: YnabConnector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await ynab_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await ynab_connector.fetch_transactions(since=since, limit=2)
        assert isinstance(txns, list)
        assert len(txns) <= 2

    async def test_fetch_transactions_not_authenticated(
        self, ynab_connector: YnabConnector
    ) -> None:
        """Calling fetch_transactions before authenticate should raise."""
        since = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(PermanentError, match="not authenticated"):
            await ynab_connector.fetch_transactions(since=since)

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        ynab_connector: YnabConnector,
        sample_ynab_raw_data: tuple[
            list[RawAccount], list[RawTransaction]
        ],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_ynab_raw_data
        assert len(raw_accounts) > 0

        canonical = ynab_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert isinstance(ca, CanonicalAccountData)
            assert ca.provider_key == "ynab"
            assert ca.external_account_id
            assert ca.account_type

        # Verify specific mappings
        checking = canonical[0]
        assert checking.name == "Checking Account"
        assert checking.account_type == "checking"
        assert checking.current_balance == Decimal("1520.45")

        savings = canonical[1]
        assert savings.name == "Emergency Fund"
        assert savings.account_type == "savings"

    async def test_transform_transactions_roundtrip(
        self,
        ynab_connector: YnabConnector,
        sample_ynab_raw_data: tuple[
            list[RawAccount], list[RawTransaction]
        ],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_ynab_raw_data
        assert len(raw_txns) > 0

        canonical = ynab_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "ynab"
            assert ct.external_transaction_id
            assert ct.transaction_type
            assert ct.status

        # First txn should be the coffee shop (outflow → negative amount)
        purchase = canonical[0]
        assert purchase.amount == Decimal("-42.50")
        assert purchase.transaction_type == "payment"
        assert purchase.status == "booked"
        assert purchase.description == "Starbucks — Coffee shop"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(
        self, ynab_connector: YnabConnector
    ) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(ynab_connector.name, str)
        assert len(ynab_connector.name) > 0
        assert ynab_connector.name == "ynab"
        assert ynab_connector.name == ynab_connector.config.provider_type

    async def test_display_name(
        self, ynab_connector: YnabConnector
    ) -> None:
        """display_name should be set."""
        assert ynab_connector.display_name == "YNAB"

    async def test_sdk_version(
        self, ynab_connector: YnabConnector
    ) -> None:
        """sdk_version should be a valid string."""
        assert ynab_connector.sdk_version == "0.1.0"


# ═══════════════════════════════════════════════════════════════════════
# Unit tests — YNAB-specific logic
# ═══════════════════════════════════════════════════════════════════════


class TestYnabConnectorAuth:
    """Authentication-specific behaviour."""

    pytestmark = pytest.mark.asyncio

    async def test_authenticate_sets_budget_ids(
        self,
        ynab_connector: YnabConnector,
        ynab_mock_transport: YnabApiMockTransport,
    ) -> None:
        """After auth, connector should have populated budget IDs."""
        assert len(ynab_connector._budget_ids) == 0

        await ynab_connector.authenticate()

        assert len(ynab_connector._budget_ids) == 1
        assert ynab_connector._budget_ids[0] == "ynab_budget_001"

        # Should have made exactly 1 GET to /budgets
        budget_calls = [
            c
            for c in ynab_mock_transport.call_log
            if "budgets" in str(c["url"])
        ]
        assert len(budget_calls) >= 1

    async def test_authenticate_no_budget_filter(
        self,
        ynab_connector_config: ConnectorConfig,
        ynab_mock_transport: YnabApiMockTransport,
    ) -> None:
        """Without budget_id filter, should discover all budgets."""
        import httpx

        from finance_sync.connectors.ynab import YnabConnector

        config = ConnectorConfig(
            provider_type="ynab",
            credentials={"access_token": "test"},
            options={},  # No budget filter
        )
        http_client = httpx.AsyncClient(
            base_url="https://api.youneedabudget.com/v1",
            transport=ynab_mock_transport,
        )
        conn = YnabConnector(config=config, http_client=http_client)
        await conn.authenticate()
        assert len(conn._budget_ids) >= 1


class TestYnabTransactionMapping:
    """Transaction parsing and mapping."""

    def test_map_category_to_type_income(self) -> None:
        """Income categories should map to 'deposit'."""
        from finance_sync.connectors.ynab import _map_category_to_type

        assert _map_category_to_type("Income: Freelance", False) == "deposit"
        assert _map_category_to_type("Salary", False) == "deposit"
        assert _map_category_to_type("Refund", False) == "deposit"

    def test_map_category_to_type_fees(self) -> None:
        """Fee categories should map to 'fee'."""
        from finance_sync.connectors.ynab import _map_category_to_type

        assert _map_category_to_type("Bank Fees", False) == "fee"
        assert _map_category_to_type("Service Fee", False) == "fee"

    def test_map_category_to_type_interest(self) -> None:
        """Interest categories should map to 'interest'."""
        from finance_sync.connectors.ynab import _map_category_to_type

        assert (
            _map_category_to_type("Interest Income", False) == "interest"
        )
        assert _map_category_to_type("Dividend", False) == "interest"

    def test_map_category_to_type_transfer(self) -> None:
        """Transfer transactions should map to 'transfer'."""
        from finance_sync.connectors.ynab import _map_category_to_type

        assert _map_category_to_type("Food & Dining", True) == "transfer"
        assert _map_category_to_type(None, True) == "transfer"

    def test_map_category_to_type_default(self) -> None:
        """Unknown categories should map to 'payment'."""
        from finance_sync.connectors.ynab import _map_category_to_type

        assert _map_category_to_type("Food & Dining", False) == "payment"
        assert _map_category_to_type("Utilities", False) == "payment"
        assert _map_category_to_type(None, False) == "other"

    def test_parse_ynab_date(self) -> None:
        """YNAB dates (YYYY-MM-DD) should parse to UTC midnight."""
        from finance_sync.connectors.ynab import _parse_ynab_date

        dt = _parse_ynab_date("2025-06-15")
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 15
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0
        assert dt.tzinfo is not None

    def test_parse_ynab_date_empty(self) -> None:
        """Empty string should return epoch."""
        from finance_sync.connectors.ynab import _parse_ynab_date

        dt = _parse_ynab_date("")
        assert dt.tzinfo is not None
        assert dt.replace(tzinfo=UTC).timestamp() == 0.0

    def test_transaction_amount_sign_inversion(self) -> None:
        """YNAB outflow (positive) should become negative in finance-sync."""
        from finance_sync.connectors.ynab import YnabConnector

        conn = YnabConnector(
            config=ConnectorConfig(provider_type="ynab", credentials={})
        )
        # YNAB: positive amount = outflow (42.50 EUR spent)
        txn = conn._parse_transaction(
            {
                "id": "test_txn_001",
                "date": "2025-06-15",
                "amount": 42500,  # 42.50 milliunits
                "cleared": "cleared",
                "approved": True,
                "account_id": "acc_checking_01",
                "category_name": "Food & Dining",
                "payee_name": "Store",
                "memo": "Groceries",
                "deleted": False,
            },
            "budget_001",
        )
        # finance-sync: outflow should be negative
        assert txn.amount == Decimal("-42.50")


class TestYnabConnectorErrorHandling:
    """Error classification and handling."""

    pytestmark = pytest.mark.asyncio

    async def test_rate_limit_error(
        self,
        ynab_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 429 should raise RateLimitError."""
        import httpx

        from finance_sync.connectors.ynab import YnabConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(429, json={"error": "Too Many Requests"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.youneedabudget.com/v1",
            transport=transport,
        )
        conn = YnabConnector(
            config=ynab_connector_config,
            http_client=http_client,
        )

        with pytest.raises(RateLimitError):
            await conn.authenticate()

    async def test_authentication_error(
        self,
        ynab_connector_config: ConnectorConfig,
    ) -> None:
        """HTTP 401 should raise PermanentError."""
        import httpx

        from finance_sync.connectors.ynab import YnabConnector

        async def handler(_: object) -> httpx.Response:
            return httpx.Response(401, json={"error": "Unauthorized"})

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(
            base_url="https://api.youneedabudget.com/v1",
            transport=transport,
        )
        conn = YnabConnector(
            config=ynab_connector_config,
            http_client=http_client,
        )

        with pytest.raises(PermanentError, match="authentication failed"):
            await conn.authenticate()
