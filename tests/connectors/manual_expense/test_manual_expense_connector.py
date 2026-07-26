"""Contract tests + unit tests for the Manual Expense connector.

Uses temporary JSON files to simulate expense data without needing
real file paths.
"""

# pyright: basic

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.models import (
    CanonicalAccountData,
    CanonicalTransactionData,
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)

if TYPE_CHECKING:
    from finance_sync.connectors.manual_expense import (
        ManualExpenseConnector,
    )


class TestManualExpenseConnectorContract:
    """Contract tests for the Manual Expense connector."""

    pytestmark = pytest.mark.asyncio

    # ── Fixtures ──────────────────────────────────────────────────────

    @pytest.fixture
    def expense_config(self) -> ConnectorConfig:
        """Return a connector config pointing at a temp JSON file."""
        expenses = {
            "expenses": [
                {
                    "id": "exp_001",
                    "date": "2025-06-15T00:00:00",
                    "amount": -45.00,
                    "currency": "EUR",
                    "description": "Lunch with team",
                    "category": "Food & Dining",
                    "tags": ["work", "team"],
                    "recurring": False,
                    "receipt_path": None,
                },
                {
                    "id": "exp_002",
                    "date": "2025-06-14T00:00:00",
                    "amount": -120.00,
                    "currency": "EUR",
                    "description": "Electric bill",
                    "category": "Utilities",
                    "tags": ["bills"],
                    "recurring": True,
                    "receipt_path": None,
                },
                {
                    "id": "exp_003",
                    "date": "2025-06-13T00:00:00",
                    "amount": 500.00,
                    "currency": "EUR",
                    "description": "Freelance payment",
                    "category": "Income",
                    "tags": ["freelance"],
                    "recurring": False,
                    "receipt_path": None,
                },
            ],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(expenses, f)
            self._data_path = f.name

        return ConnectorConfig(
            provider_type="manual_expense",
            credentials={},
            options={
                "data_path": self._data_path,
                "default_currency": "EUR",
                "account_name": "Test Wallet",
            },
        )

    @pytest.fixture
    def expense_connector(
        self,
        expense_config: ConnectorConfig,
    ) -> ManualExpenseConnector:
        """Return a ManualExpenseConnector with the test config."""
        from finance_sync.connectors.manual_expense import (
            ManualExpenseConnector,
        )

        return ManualExpenseConnector(config=expense_config)

    @pytest.fixture
    def sample_expense_raw_data(
        self,
    ) -> tuple[list[RawAccount], list[RawTransaction]]:
        """Return sample data for transform tests."""
        return [
            RawAccount(
                external_account_id="manual_wallet",
                name="Test Wallet",
                account_type="checking",
                currency_code="EUR",
            ),
        ], [
            RawTransaction(
                external_transaction_id="manual_exp_001",
                external_account_id="manual_wallet",
                amount=Decimal("-45.00"),
                currency_code="EUR",
                occurred_at=datetime(2025, 6, 15, tzinfo=UTC),
                description="Lunch with team [Food & Dining] #work #team",
                transaction_type="expense",
                status="booked",
            ),
        ]

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """Connector should authenticate without raising."""
        await expense_connector.authenticate()

    async def test_authenticate_idempotent(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await expense_connector.authenticate()
        await expense_connector.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await expense_connector.health()
        assert health.provider_type == expense_connector.name

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await expense_connector.authenticate()
        accounts = await expense_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) == 1
        assert isinstance(accounts[0], RawAccount)
        assert accounts[0].external_account_id == "manual_wallet"
        assert accounts[0].name == "Test Wallet"

    async def test_fetch_accounts_idempotent(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await expense_connector.authenticate()
        first = await expense_connector.fetch_accounts()
        second = await expense_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """fetch_transactions should return parsed expenses."""
        await expense_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await expense_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) == 3  # 3 expenses in fixture

        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id == "manual_exp_001"
        assert txn.amount == Decimal("-45.00")
        assert txn.description == (
            "Lunch with team [Food & Dining] #work #team"
        )

    async def test_fetch_transactions_with_since_filter(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """fetch_transactions should filter by 'since' date."""
        await expense_connector.authenticate()
        since = datetime(2025, 6, 14, tzinfo=UTC)
        txns = await expense_connector.fetch_transactions(since=since)
        assert len(txns) == 2  # 2 on/after June 14

    async def test_fetch_transactions_with_limit(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await expense_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await expense_connector.fetch_transactions(since=since, limit=2)
        assert isinstance(txns, list)
        assert len(txns) <= 2

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        expense_connector: ManualExpenseConnector,
        sample_expense_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_expense_raw_data
        canonical = expense_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert isinstance(ca, CanonicalAccountData)
            assert ca.provider_key == "manual_expense"

    async def test_transform_transactions_roundtrip(
        self,
        expense_connector: ManualExpenseConnector,
        sample_expense_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_expense_raw_data
        canonical = expense_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "manual_expense"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(expense_connector.name, str)
        assert expense_connector.name == "manual_expense"

    async def test_display_name(
        self, expense_connector: ManualExpenseConnector
    ) -> None:
        """display_name should be set."""
        assert expense_connector.display_name == "Manual Expenses"

    # ── Cleanup ────────────────────────────────────────────────────────

    def teardown_method(self) -> None:
        """Clean up the temp file."""
        import os

        if hasattr(self, "_data_path") and os.path.exists(self._data_path):
            os.unlink(self._data_path)
