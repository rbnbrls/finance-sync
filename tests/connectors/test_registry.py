"""Tests for the ConnectorRegistry."""
# pyright: basic

from __future__ import annotations

import pytest

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry


class TestConnectorRegistryEmpty:
    """Tests against a fresh registry with built-in connectors."""

    def test_available_empty(self) -> None:
        registry = ConnectorRegistry()
        assert registry.available == ["bunq", "trading212"]

    def test_len_empty(self) -> None:
        registry = ConnectorRegistry()
        assert len(registry) == 2

    def test_contains_false(self) -> None:
        registry = ConnectorRegistry()
        assert "nonexistent" not in registry

    def test_get_connector_empty(self) -> None:
        registry = ConnectorRegistry()
        config = ConnectorConfig(provider_type="nonexistent")
        with pytest.raises(PermanentError, match="Unknown connector"):
            registry.get_connector(config)


class TestConnectorRegistryWithMock:
    """Tests against a registry with MockConnector registered."""

    def test_register_and_list(self, registry_with_mock: tuple) -> None:
        registry, _ = registry_with_mock
        assert "mock_provider" in registry
        # 2 built-in (bunq, trading212) + 1 mock = 3
        assert len(registry) == 3

        metadata = registry.list_connectors()
        assert "mock_provider" in metadata
        assert metadata["mock_provider"]["sdk_version"] == "0.1.0"

    def test_get_connector_success(self, registry_with_mock: tuple) -> None:
        registry, config = registry_with_mock
        connector = registry.get_connector(config)
        assert isinstance(connector, Connector)
        assert connector.name == "mock_provider"

    def test_duplicate_register_raises(self, registry_with_mock: tuple) -> None:
        registry, _ = registry_with_mock
        from tests.conftest import MockConnector

        with pytest.raises(ValueError, match="already registered"):
            registry.register_class("mock_provider", MockConnector)

    def test_duplicate_register_with_replace(
        self, registry_with_mock: tuple
    ) -> None:
        registry, config = registry_with_mock
        from tests.conftest import MockConnector

        registry.register_class("mock_provider", MockConnector, replace=True)
        connector = registry.get_connector(config)
        assert connector.name == "mock_provider"

    def test_list_connectors_metadata(self, registry_with_mock: tuple) -> None:
        registry, _ = registry_with_mock
        metadata = registry.list_connectors()
        mock_meta = metadata["mock_provider"]
        assert mock_meta["name"] == "mock_provider"
        assert mock_meta["display_name"] == "Mock Provider (Test)"
        assert mock_meta["has_rate_limit_policy"] is False

    def test_get_unknown_connector(self, registry_with_mock: tuple) -> None:
        registry, _ = registry_with_mock
        config = ConnectorConfig(provider_type="unknown_provider")
        with pytest.raises(PermanentError, match="unknown_provider"):
            registry.get_connector(config)

    def test_reload_clears_programmatic_registrations(
        self, registry_with_mock: tuple
    ) -> None:
        """Reload re-scans entry points, losing programmatic registrations."""
        registry, _config = registry_with_mock
        registry.reload()
        # After reload, programmatic registrations are gone
        # (no entry points exist in the test environment)
        assert "mock_provider" not in registry


class TestConnectorRegistryRegisterInvalid:
    """Registering invalid types."""

    def test_register_non_connector_class(self) -> None:
        registry = ConnectorRegistry()

        class NotAConnector:
            pass

        with pytest.raises(TypeError, match="Connector subclass"):
            registry.register_class("bad", NotAConnector)  # type: ignore[arg-type]

    def test_register_instance(self) -> None:
        registry = ConnectorRegistry()
        with pytest.raises(TypeError):
            registry.register_class("bad", "not_a_class")  # type: ignore[arg-type]
