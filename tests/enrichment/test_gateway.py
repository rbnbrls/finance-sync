"""Tests for the EnrichmentGateway service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.gateway import EnrichmentGateway, _safe_decimal
from finance_sync.enrichment.models import PriceObservation


class TestSafeDecimal:
    """Unit tests for the _safe_decimal helper."""

    def test_none(self) -> None:
        assert _safe_decimal(None) is None

    def test_decimal(self) -> None:
        assert _safe_decimal(Decimal("10.5")) == Decimal("10.5")

    def test_float(self) -> None:
        assert _safe_decimal(10.5) == Decimal("10.5")

    def test_string(self) -> None:
        assert _safe_decimal("10.5") == Decimal("10.5")

    def test_invalid(self) -> None:
        assert _safe_decimal("not-a-number") is None
        assert _safe_decimal({}) is None


class TestEnrichmentGatewayDegraded:
    """Tests for EnrichmentGateway in degraded mode (no API key)."""

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.openbb_api_key = None
        s.openbb_base_url = "https://openbb.co/api"
        s.openbb_api_version = "v1"
        s.openbb_request_timeout = 30
        return s

    @pytest.fixture
    def mock_uow(self):
        uow = MagicMock()
        uow.enrichment_freshness = AsyncMock()
        uow.securities = AsyncMock()
        return uow

    @pytest.fixture
    def mock_price_store(self):
        return AsyncMock()

    @pytest.fixture
    def mock_resolver(self):
        return AsyncMock()

    @pytest.fixture
    def gateway(self, settings, mock_uow, mock_price_store, mock_resolver):
        return EnrichmentGateway(
            settings=settings,
            uow=mock_uow,
            price_store=mock_price_store,
            resolver=mock_resolver,
        )

    def test_is_degraded(self, gateway) -> None:
        """Gateway is degraded when no API key is set."""
        assert gateway.is_degraded

    async def test_resolve_security_degraded(self, gateway) -> None:
        """resolve_security returns None in degraded mode."""
        result = await gateway.resolve_security("AAPL", "ticker")
        assert result is None

    async def test_get_quote_degraded_fallback(
        self, gateway, mock_price_store
    ) -> None:
        """get_latest_quote falls back to local price store."""
        mock_price_store.get_latest_price.return_value = PriceObservation(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            price_close=Decimal("194.30"),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )

        quote = await gateway.get_latest_quote(
            security_id="sec_1",
            identifier="AAPL",
        )
        assert quote is not None
        assert quote.price == Decimal("194.30")
        assert quote.source == "local"

    async def test_get_quote_degraded_no_data(
        self, gateway, mock_price_store
    ) -> None:
        """get_latest_quote returns None when no local data."""
        mock_price_store.get_latest_price.return_value = None

        quote = await gateway.get_latest_quote(
            security_id="sec_1",
            identifier="AAPL",
        )
        assert quote is None

    async def test_get_historical_prices_degraded(
        self, gateway, mock_price_store
    ) -> None:
        """get_historical_prices returns local data in degraded mode."""
        mock_price_store.get_price_history.return_value = [
            PriceObservation(
                security_id="sec_1",
                timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
                price_close=Decimal("194.30"),
                source="openbb",
                interval="1d",
                currency_code="USD",
            )
        ]

        result = await gateway.get_historical_prices(
            security_id="sec_1",
            identifier="AAPL",
        )
        assert len(result) == 1
        assert result[0].price_close == Decimal("194.30")

    async def test_update_freshness_new(self, gateway, mock_uow) -> None:
        """update_freshness creates a new record when none exists."""
        mock_uow.enrichment_freshness.list = AsyncMock(return_value=[])
        mock_uow.enrichment_freshness.add = AsyncMock()

        await gateway.update_freshness(
            security_id="sec_1",
            field="last_quote_fetch",
            status="resolved",
        )
        mock_uow.enrichment_freshness.add.assert_awaited_once()

    async def test_update_freshness_existing(self, gateway, mock_uow) -> None:
        """update_freshness updates existing record."""
        existing = MagicMock()
        existing.last_quote_fetch = None
        existing.status = "pending"
        existing.error_message = None
        mock_uow.enrichment_freshness.list = AsyncMock(return_value=[existing])
        mock_uow.enrichment_freshness.update = AsyncMock()

        await gateway.update_freshness(
            security_id="sec_1",
            field="last_quote_fetch",
            status="resolved",
        )
        assert existing.status == "resolved"
        mock_uow.enrichment_freshness.update.assert_awaited_once_with(existing)

    async def test_close(self, gateway) -> None:
        """Closing the gateway cleans up the HTTP client."""
        mock_client = AsyncMock()
        mock_client.is_closed = False
        gateway._http_client = mock_client
        await gateway.close()
        mock_client.aclose.assert_awaited_once()

    async def test_close_no_client(self, gateway) -> None:
        """Closing without an HTTP client is a no-op."""
        await gateway.close()  # should not raise


class TestEnrichmentGatewayWithApiKey:
    """Tests for EnrichmentGateway with an API key."""

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.openbb_api_key = MagicMock()
        s.openbb_api_key.get_secret_value.return_value = "sk-test-key"
        s.openbb_base_url = "https://openbb.co/api"
        s.openbb_api_version = "v1"
        s.openbb_request_timeout = 30
        s.price_store_keep_minute_days = 30
        s.price_store_keep_hour_days = 90
        return s

    @pytest.fixture
    def mock_uow(self):
        uow = MagicMock()
        uow.enrichment_freshness = AsyncMock()
        uow.enrichment_freshness.list = AsyncMock(return_value=[])
        uow.enrichment_freshness.add = AsyncMock()
        return uow

    @pytest.fixture
    def mock_price_store(self):
        store = AsyncMock()
        store.get_latest_price = AsyncMock(return_value=None)
        store.get_price_history = AsyncMock(return_value=[])
        store.store_prices = AsyncMock(return_value=1)
        return store

    @pytest.fixture
    def mock_resolver(self):
        r = AsyncMock()
        r.resolve_security = AsyncMock()
        return r

    @pytest.fixture
    def gateway(self, settings, mock_uow, mock_price_store, mock_resolver):
        g = EnrichmentGateway(
            settings=settings,
            uow=mock_uow,
            price_store=mock_price_store,
            resolver=mock_resolver,
        )
        # Mock the HTTP client so we control responses
        mock_client = AsyncMock()
        mock_client.is_closed = False
        # Make get async so it can be awaited
        mock_client.get = AsyncMock()
        g._http_client = mock_client
        return g

    def _make_mock_response(self, status_code=200, json_data=None):
        """Helper to create a mock httpx Response."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.json.return_value = json_data or {}
        if status_code >= 400:
            import httpx

            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Error", request=MagicMock(), response=mock_response
            )
        else:
            mock_response.raise_for_status = MagicMock()
        return mock_response

    def test_not_degraded(self, gateway) -> None:
        """Gateway is not degraded when API key is set."""
        assert not gateway.is_degraded

    async def test_resolve_security_api_call(self, gateway) -> None:
        """resolve_security makes an API call when not degraded."""
        mock_response = self._make_mock_response(
            json_data={
                "isin": "US0378331005",
                "figi": "BBG000B9XRY4",
                "ticker": "AAPL",
                "name": "Apple Inc.",
                "currency": "USD",
            }
        )
        gateway._http_client.get.return_value = mock_response

        result = await gateway.resolve_security("AAPL", "ticker")
        assert result is not None
        assert result.isin == "US0378331005"
        assert result.name == "Apple Inc."
        assert result.source == "openbb"

    async def test_resolve_security_not_found(self, gateway) -> None:
        """resolve_security returns None on 404."""
        mock_response = self._make_mock_response(status_code=404, json_data={})
        gateway._http_client.get.return_value = mock_response

        result = await gateway.resolve_security("UNKNOWN", "ticker")
        assert result is None

    async def test_get_quote_with_api_key(
        self, gateway, mock_price_store
    ) -> None:
        """get_latest_quote fetches from OpenBB when API key is set."""
        mock_response = self._make_mock_response(
            json_data={
                "price": 194.30,
                "change": 2.50,
                "changePercent": 1.30,
                "currency": "USD",
            }
        )
        gateway._http_client.get.return_value = mock_response

        quote = await gateway.get_latest_quote(
            security_id="sec_1",
            identifier="AAPL",
        )
        assert quote is not None
        assert quote.price == Decimal("194.30")
        assert quote.source == "openbb"
