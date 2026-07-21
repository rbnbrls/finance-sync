"""
Integration test for the finance-sync-sdk plugin system.

Tests that plugins can be registered, discovered, and loaded via the
PluginRegistry, including an example connector loaded from a separate
module path (simulating a third-party package).
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from finance_sync_sdk.models import ConnectorConfig
from finance_sync_sdk.registry import PluginRegistry


class TestPluginIntegration:
    """Integration tests for the full plugin lifecycle."""

    def setup_method(self) -> None:
        self.registry = PluginRegistry()

    async def test_register_example_connector(self):
        """Register and use the Plaid-like connector via registry."""
        from examples.plaid_like_connector import PlaidLikeConnector

        self.registry.register_connector(
            "plaid_like", PlaidLikeConnector, replace=True
        )

        config = ConnectorConfig(
            provider_type="plaid_like",
            credentials={"client_id": "test", "access_token": "test_access"},
            options={"environment": "sandbox"},
        )
        plugin = self.registry.get_connector(config)
        assert plugin.name == "plaid_like"
        assert plugin.display_name == "Plaid-like Open Banking"

        await plugin.authenticate()
        assert plugin._authenticated

        accounts = await plugin.fetch_accounts()
        assert len(accounts) >= 3  # checking, savings, credit card
        assert accounts[0].external_account_id.startswith("plaid_acc_")

        since = datetime.now(UTC) - timedelta(days=30)
        txns = await plugin.fetch_transactions(since)
        assert len(txns) >= 1

    async def test_csv_import_connector_with_real_csv(self):
        """Test CSV import connector reads a real CSV file."""
        from examples.csv_import_connector import CSVImportConnector

        self.registry.register_connector(
            "csv_import", CSVImportConnector, replace=True
        )

        # Create a temp CSV file
        import csv
        import os

        fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix="test_import_")
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Description", "Amount (EUR)"])
            writer.writerow(["2025-01-15", "Supermarket purchase", "-45.50"])
            writer.writerow(["2025-01-20", "Salary deposit", "2500.00"])
            writer.writerow(["2025-02-01", "Streaming subscription", "-12.99"])

        try:
            config = ConnectorConfig(
                provider_type="csv_import",
                credentials={},
                options={
                    "csv_path": csv_path,
                    "column_mapping": {
                        "date": "Date",
                        "description": "Description",
                        "amount": "Amount (EUR)",
                    },
                    "account_name": "Test CSV Account",
                },
            )
            plugin = self.registry.get_connector(config)

            await plugin.authenticate()
            accounts = await plugin.fetch_accounts()
            assert len(accounts) == 1
            assert accounts[0].name == "Test CSV Account"

            since = datetime(2025, 1, 1, tzinfo=UTC)
            txns = await plugin.fetch_transactions(since)
            assert len(txns) == 3

            # Check ordering/amounts
            assert txns[0].description == "Supermarket purchase"
            assert txns[0].amount == -45.50
            assert txns[1].amount == 2500.00
            assert txns[2].description == "Streaming subscription"
        finally:
            os.unlink(csv_path)

    async def test_csv_import_with_since_filter(self):
        """CSV import respects the 'since' filter."""
        from examples.csv_import_connector import CSVImportConnector

        self.registry.register_connector(
            "csv_import", CSVImportConnector, replace=True
        )

        import csv
        import os

        fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix="test_filter_")
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Amount"])
            writer.writerow(["2025-01-01", "100.00"])
            writer.writerow(["2025-06-01", "200.00"])
            writer.writerow(["2025-12-01", "300.00"])

        try:
            config = ConnectorConfig(
                provider_type="csv_import",
                credentials={},
                options={
                    "csv_path": csv_path,
                    "column_mapping": {
                        "date": "Date",
                        "amount": "Amount",
                    },
                },
            )
            plugin = self.registry.get_connector(config)

            await plugin.authenticate()
            since = datetime(2025, 6, 1, tzinfo=UTC)
            txns = await plugin.fetch_transactions(since)
            # Should only return 2025-06-01 and 2025-12-01
            assert len(txns) == 2
        finally:
            os.unlink(csv_path)

    async def test_manual_expense_connector(self):
        """Manual expense connector reads from a JSON file."""
        from examples.manual_expense_connector import ManualExpenseConnector

        self.registry.register_connector(
            "manual_expense", ManualExpenseConnector, replace=True
        )

        import json
        import os

        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="test_expense_")
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "expenses": [
                        {
                            "id": "exp_001",
                            "date": "2025-01-15",
                            "amount": -45.00,
                            "currency": "EUR",
                            "description": "Lunch",
                            "category": "Food & Dining",
                            "tags": ["work"],
                            "recurring": False,
                            "receipt_path": None,
                        },
                        {
                            "id": "exp_002",
                            "date": "2025-02-01",
                            "amount": 1500.00,
                            "currency": "EUR",
                            "description": "Freelance payment",
                            "category": "Income",
                            "tags": ["freelance"],
                            "recurring": False,
                            "receipt_path": None,
                        },
                    ]
                },
                f,
                indent=2,
            )

        try:
            config = ConnectorConfig(
                provider_type="manual_expense",
                credentials={},
                options={"data_path": json_path},
            )
            plugin = self.registry.get_connector(config)

            await plugin.authenticate()
            since = datetime(2025, 1, 1, tzinfo=UTC)
            txns = await plugin.fetch_transactions(since)
            assert len(txns) == 2
            assert txns[0].description.startswith("Lunch")
            assert txns[0].transaction_type == "expense"
            assert txns[1].transaction_type == "income"
        finally:
            os.unlink(json_path)

    async def test_plugin_registry_round_trip(self):
        """Full round-trip: register → list → get → use."""
        from examples.plaid_like_connector import PlaidLikeConnector

        self.registry.register_connector(
            "plaid_like_test", PlaidLikeConnector, replace=True
        )

        # List
        plugins = self.registry.list_connectors()
        assert "plaid_like_test" in plugins
        assert (
            plugins["plaid_like_test"]["display_name"]
            == "Plaid-like Open Banking"
        )

        # Contains
        assert "plaid_like_test" in self.registry

        # Get and use
        config = ConnectorConfig(
            provider_type="plaid_like_test",
            credentials={"access_token": "test"},
            options={"environment": "sandbox"},
        )
        plugin = self.registry.get_connector(config)
        assert plugin.name == "plaid_like"

    async def test_unknown_connector_raises(self):
        """Getting an unregistered connector raises RuntimeError."""
        config = ConnectorConfig(provider_type="does_not_exist")
        with pytest.raises(RuntimeError, match="Unknown connector"):
            self.registry.get_connector(config)

    async def test_csv_import_with_directory(self):
        """CSV import from a directory of CSV files."""
        from examples.csv_import_connector import CSVImportConnector

        self.registry.register_connector(
            "csv_import", CSVImportConnector, replace=True
        )

        import os
        import tempfile

        temp_dir = tempfile.mkdtemp(prefix="csv_dir_")

        try:
            # Create two CSV files
            for i, name in enumerate(["bank1.csv", "bank2.csv"]):
                with open(os.path.join(temp_dir, name), "w") as f:
                    f.write(f"Date,Amount\n2025-01-{10 + i},50.00\n")

            config = ConnectorConfig(
                provider_type="csv_import",
                credentials={},
                options={"csv_directory": temp_dir},
            )
            plugin = self.registry.get_connector(config)

            await plugin.authenticate()
            since = datetime(2025, 1, 1, tzinfo=UTC)
            txns = await plugin.fetch_transactions(since)
            assert len(txns) == 2
        finally:
            import shutil

            shutil.rmtree(temp_dir)
