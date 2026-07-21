"""Tests for finance-sync-sdk plugin base classes and registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from finance_sync_sdk import (
    ConnectorPlugin,
    ExporterPlugin,
    PluginRegistry,
)
from finance_sync_sdk.exceptions import PermanentError, TransientError
from finance_sync_sdk.models import (
    ConnectorConfig,
    ExportRequest,
    ExportResult,
    RawAccount,
    RawTransaction,
)
from finance_sync_sdk.rate_limiter import RateLimitPolicy

# ── Test helpers ────────────────────────────────────────────────────────


class GoodTestPlugin(ConnectorPlugin):
    """Minimal working connector for tests."""

    display_name = "Test Plugin"
    plugin_version = "1.0.0"

    @property
    def name(self) -> str:
        return "test_plugin"

    async def authenticate(self) -> None:
        if not self.config.credentials.get("api_key"):
            raise PermanentError("api_key required")
        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        return [
            RawAccount(
                external_account_id="acc_1",
                name="Test Checking",
                account_type="checking",
            )
        ]

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        return [
            RawTransaction(
                external_transaction_id="tx_1",
                external_account_id="acc_1",
                amount=Decimal("-50.00"),
                occurred_at=since,
            )
        ]


class FailingAuthPlugin(ConnectorPlugin):
    """Connector that always fails authentication."""

    @property
    def name(self) -> str:
        return "failing_auth"

    async def authenticate(self) -> None:
        raise PermanentError("Invalid credentials")

    async def fetch_accounts(self) -> list[RawAccount]:
        return []

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        return []


class TransientPlugin(ConnectorPlugin):
    """Connector with transient errors for retry testing."""

    rate_limit_policy = RateLimitPolicy(max_requests=10, max_retries=2)

    @property
    def name(self) -> str:
        return "transient_plugin"

    async def authenticate(self) -> None:
        self._authenticated = True

    async def fetch_accounts(self) -> list[RawAccount]:
        raise TransientError("Temporary outage")

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        return []


class CSVExporterPlugin(ExporterPlugin):
    """Minimal exporter for tests."""

    display_name = "CSV Exporter"
    supported_formats = ["csv"]

    @property
    def name(self) -> str:
        return "csv_exporter"

    async def export(self, request: ExportRequest) -> ExportResult:
        content = "date,amount,description\n2025-01-01,100.00,Test"
        return ExportResult(
            status="completed",
            records_exported=1,
        )


# ── Tests ───────────────────────────────────────────────────────────────


class TestConnectorPlugin:
    async def test_successful_auth_and_fetch(self):
        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = GoodTestPlugin(config=config)
        assert plugin.name == "test_plugin"
        assert not plugin._authenticated

        await plugin.authenticate()
        assert plugin._authenticated

        accounts = await plugin.fetch_accounts()
        assert len(accounts) == 1
        assert accounts[0].external_account_id == "acc_1"

        since = datetime.now(UTC) - timedelta(days=7)
        txns = await plugin.fetch_transactions(since)
        assert len(txns) == 1
        assert txns[0].external_transaction_id == "tx_1"

    async def test_authentication_failure(self):
        config = ConnectorConfig(provider_type="failing_auth")
        plugin = FailingAuthPlugin(config=config)
        with pytest.raises(PermanentError, match="Invalid credentials"):
            await plugin.authenticate()

    async def test_health_check_healthy(self):
        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = GoodTestPlugin(config=config)
        health = await plugin.health()
        assert health.healthy
        assert health.provider_type == "test_plugin"

    async def test_health_check_unhealthy(self):
        config = ConnectorConfig(provider_type="failing_auth")
        plugin = FailingAuthPlugin(config=config)
        health = await plugin.health()
        assert not health.healthy
        assert "Invalid credentials" in (health.message or "")

    async def test_transform_accounts(self):
        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = GoodTestPlugin(config=config)
        raw = [
            RawAccount(
                external_account_id="acc_1",
                name="Test Account",
                account_type="checking",
                current_balance=Decimal("1000.00"),
            )
        ]
        canonical = plugin.transform_accounts(raw)
        assert len(canonical) == 1
        assert canonical[0].provider_key == "test_plugin"
        assert canonical[0].current_balance == Decimal("1000.00")

    async def test_transform_transactions(self):
        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = GoodTestPlugin(config=config)
        now = datetime.now(UTC)
        raw = [
            RawTransaction(
                external_transaction_id="tx_1",
                external_account_id="acc_1",
                amount=Decimal("-50.00"),
                occurred_at=now,
                transaction_type="payment",
            )
        ]
        canonical = plugin.transform_transactions(raw)
        assert len(canonical) == 1
        assert canonical[0].transaction_type == "payment"
        assert canonical[0].status == "pending"

    async def test_retry_on_transient_error(self):
        config = ConnectorConfig(provider_type="transient_plugin")
        plugin = TransientPlugin(config=config)
        with pytest.raises(TransientError, match="retries exhausted"):
            await plugin._rate_limited_fetch_accounts()

    async def test_describe(self):
        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = GoodTestPlugin(config=config)
        meta = plugin.describe()
        assert meta["name"] is None  # not set as class attr
        assert meta["display_name"] == "Test Plugin"
        assert meta["plugin_version"] == "1.0.0"


class TestExporterPlugin:
    async def test_export_csv(self):
        exporter = CSVExporterPlugin()
        request = ExportRequest(format="csv")
        result = await exporter.run_export(request)
        assert result.status == "completed"
        assert result.records_exported == 1

    async def test_unsupported_format(self):
        exporter = CSVExporterPlugin()
        request = ExportRequest(format="json")
        result = await exporter.run_export(request)
        assert result.status == "failed"
        assert "not supported" in (result.error_message or "")

    async def test_name(self):
        exporter = CSVExporterPlugin()
        assert exporter.name == "csv_exporter"

    async def test_describe(self):
        meta = CSVExporterPlugin.describe()
        assert meta["display_name"] == "CSV Exporter"
        assert meta["supported_formats"] == ["csv"]


class TestPluginRegistry:
    def setup_method(self) -> None:
        self.registry = PluginRegistry()

    def test_register_and_get_connector(self):
        self.registry.register_connector("test_plugin", GoodTestPlugin)
        assert "test_plugin" in self.registry.available_connectors

        config = ConnectorConfig(
            provider_type="test_plugin",
            credentials={"api_key": "sk_test_123"},
        )
        plugin = self.registry.get_connector(config)
        assert isinstance(plugin, GoodTestPlugin)
        assert plugin.name == "test_plugin"

    def test_register_duplicate_raises(self):
        self.registry.register_connector("test_plugin", GoodTestPlugin)
        with pytest.raises(ValueError, match="already registered"):
            self.registry.register_connector("test_plugin", GoodTestPlugin)

    def test_register_duplicate_with_replace(self):
        self.registry.register_connector("test_plugin", GoodTestPlugin)
        self.registry.register_connector("test_plugin", GoodTestPlugin, replace=True)
        assert "test_plugin" in self.registry.available_connectors

    def test_register_and_get_exporter(self):
        self.registry.register_exporter("csv_exporter", CSVExporterPlugin)
        assert "csv_exporter" in self.registry.available_exporters

        exporter = self.registry.get_exporter("csv_exporter")
        assert isinstance(exporter, CSVExporterPlugin)

    def test_get_unknown_connector_raises(self):
        config = ConnectorConfig(provider_type="nonexistent")
        with pytest.raises(RuntimeError, match="Unknown connector"):
            self.registry.get_connector(config)

    def test_list_connectors(self):
        self.registry.register_connector("test_plugin", GoodTestPlugin)
        plugins = self.registry.list_connectors()
        assert "test_plugin" in plugins
        assert plugins["test_plugin"]["display_name"] == "Test Plugin"

    def test_list_exporters(self):
        self.registry.register_exporter("csv_exporter", CSVExporterPlugin)
        exporters = self.registry.list_exporters()
        assert "csv_exporter" in exporters

    def test_len_and_contains(self):
        self.registry.register_connector("test_plugin", GoodTestPlugin)
        self.registry.register_exporter("csv_exporter", CSVExporterPlugin)
        assert len(self.registry) == 2
        assert "test_plugin" in self.registry
        assert "csv_exporter" in self.registry
        assert "nonexistent" not in self.registry
