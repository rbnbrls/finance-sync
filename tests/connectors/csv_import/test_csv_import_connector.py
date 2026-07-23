"""Contract tests + unit tests for the CSV Import connector.

Uses temporary CSV files to simulate CSV data without needing
real file paths.
"""

# pyright: basic

from __future__ import annotations

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
    from finance_sync.connectors.csv_import import CSVImportConnector


class TestCSVImportConnectorContract:
    """Contract tests for the CSV Import connector."""

    pytestmark = pytest.mark.asyncio

    # ── Fixtures ──────────────────────────────────────────────────────

    @pytest.fixture
    def csv_config(self) -> ConnectorConfig:
        """Return a basic CSV connector config pointing at a temp file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write("Date,Description,Amount\n")
            f.write("2025-06-15,Coffee shop,-42.50\n")
            f.write("2025-06-14,Salary,3000.00\n")
            f.write("2025-06-13,Rent,-1200.00\n")
            self._csv_path = f.name

        return ConnectorConfig(
            provider_type="csv_import",
            credentials={},
            options={
                "csv_path": self._csv_path,
                "date_format": "%Y-%m-%d",
                "column_mapping": {
                    "date": "Date",
                    "description": "Description",
                    "amount": "Amount",
                },
                "currency": "EUR",
                "account_name": "Test CSV Account",
            },
        )

    @pytest.fixture
    def csv_connector(self, csv_config: ConnectorConfig) -> CSVImportConnector:
        """Return a CSVImportConnector with the test config."""
        from finance_sync.connectors.csv_import import CSVImportConnector

        return CSVImportConnector(config=csv_config)

    @pytest.fixture
    def sample_csv_raw_data(
        self,
    ) -> tuple[list[RawAccount], list[RawTransaction]]:
        """Return sample data for transform tests."""
        return [
            RawAccount(
                external_account_id="csv_default",
                name="Test Import",
                account_type="checking",
                currency_code="EUR",
            ),
        ], [
            RawTransaction(
                external_transaction_id="csv_test.csv_1",
                external_account_id="csv_test.csv",
                amount=Decimal("-42.50"),
                currency_code="EUR",
                occurred_at=datetime(2025, 6, 15, tzinfo=UTC),
                description="Coffee shop",
                transaction_type="debit",
                status="booked",
            ),
        ]

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """Connector should authenticate without raising."""
        await csv_connector.authenticate()

    async def test_authenticate_idempotent(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """Calling authenticate twice should be safe."""
        await csv_connector.authenticate()
        await csv_connector.authenticate()

    async def test_authenticate_missing_path(self) -> None:
        """Missing csv_path/csv_directory should raise PermanentError."""
        from finance_sync.connectors.csv_import import CSVImportConnector

        config = ConnectorConfig(provider_type="csv_import")
        conn = CSVImportConnector(config)
        with pytest.raises(Exception, match="csv_path"):
            await conn.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await csv_connector.health()
        assert health.provider_type == csv_connector.name

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await csv_connector.authenticate()
        accounts = await csv_connector.fetch_accounts()
        assert isinstance(accounts, list)
        assert len(accounts) == 1
        assert isinstance(accounts[0], RawAccount)
        assert accounts[0].external_account_id == "csv_default"
        assert accounts[0].name == "Test CSV Account"

    async def test_fetch_accounts_idempotent(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """Calling fetch_accounts twice should be safe."""
        await csv_connector.authenticate()
        first = await csv_connector.fetch_accounts()
        second = await csv_connector.fetch_accounts()
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert len(first) == len(second)

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """fetch_transactions should return parsed CSV transactions."""
        await csv_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await csv_connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        assert len(txns) == 3  # 3 rows in test CSV

        txn = txns[0]
        assert isinstance(txn, RawTransaction)
        assert txn.external_transaction_id
        assert txn.external_account_id
        assert txn.amount is not None

    async def test_fetch_transactions_with_since_filter(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """fetch_transactions should filter by 'since' date."""
        await csv_connector.authenticate()
        since = datetime(2025, 6, 14, tzinfo=UTC)
        txns = await csv_connector.fetch_transactions(since=since)
        # Only 2 transactions on or after June 14
        assert len(txns) == 2

    async def test_fetch_transactions_with_limit(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await csv_connector.authenticate()
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await csv_connector.fetch_transactions(since=since, limit=2)
        assert isinstance(txns, list)
        assert len(txns) <= 2

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        csv_connector: CSVImportConnector,
        sample_csv_raw_data: tuple[
            list[RawAccount], list[RawTransaction]
        ],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_csv_raw_data
        canonical = csv_connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert isinstance(ca, CanonicalAccountData)
            assert ca.provider_key == "csv_import"

    async def test_transform_transactions_roundtrip(
        self,
        csv_connector: CSVImportConnector,
        sample_csv_raw_data: tuple[
            list[RawAccount], list[RawTransaction]
        ],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_csv_raw_data
        canonical = csv_connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert isinstance(ct, CanonicalTransactionData)
            assert ct.provider_key == "csv_import"

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(csv_connector.name, str)
        assert csv_connector.name == "csv_import"
        assert csv_connector.name == csv_connector.config.provider_type

    async def test_display_name(
        self, csv_connector: CSVImportConnector
    ) -> None:
        """display_name should be set."""
        assert csv_connector.display_name == "CSV File Import"

    # ── Cleanup ────────────────────────────────────────────────────────

    def teardown_method(self) -> None:
        """Clean up the temp file."""
        import os

        if hasattr(self, "_csv_path") and os.path.exists(self._csv_path):
            os.unlink(self._csv_path)
