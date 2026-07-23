"""Tests for the FxService — FX rate fetching, caching, and conversion."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import (
    FxConversionRequest,
    FxRateObservation,
)
from finance_sync.services.fx_service import FxService


class TestFxServiceDegraded:
    """Tests for FxService in degraded mode (no API key)."""

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
        uow.fx_rates = AsyncMock()
        uow.fx_rates.list = AsyncMock(return_value=[])
        uow.fx_rates.add = AsyncMock()
        return uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    def test_is_degraded(self, service) -> None:
        """Service is degraded when no API key is set."""
        assert service._degraded

    async def test_get_rate_same_currency(self, service) -> None:
        """get_rate returns identity rate for same currency."""
        result = await service.get_rate("EUR", "EUR")
        assert result is not None
        assert result.rate == Decimal(1)
        assert result.source == "identity"

    async def test_get_rate_no_data(self, service) -> None:
        """get_rate returns None when no data and no API key."""
        result = await service.get_rate("EUR", "USD")
        assert result is None

    async def test_get_rate_from_local(self, service, mock_uow) -> None:
        """get_rate returns cached rate from local DB."""
        # Simulate a row returned as a MagicMock that looks like an ORM row
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.0945")
        assert result.source == "openbb"

    async def test_get_inverted_local_rate(self, service, mock_uow) -> None:
        """get_rate inverts cached rate when stored in opposite direction."""
        mock_row = MagicMock()
        mock_row.base_currency = "USD"
        mock_row.quote_currency = "EUR"
        mock_row.rate = Decimal("0.9140")
        mock_row.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_row.source = "openbb"

        # First call looks up EUR/USD (empty), second USD/EUR (found)
        mock_uow.fx_rates.list = AsyncMock(side_effect=[[], [mock_row]])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        # Inverse of 0.9140 should give us roughly 1.0941
        expected = round(Decimal(1) / Decimal("0.9140"), 12)
        assert result.rate == expected
        assert result.base_currency == "EUR"
        assert result.quote_currency == "USD"

    async def test_convert_same_currency(self, service) -> None:
        """convert returns identity for same-currency conversion."""
        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="EUR",
            amount=Decimal("100.00"),
        )
        result = await service.convert(request)
        assert result is not None
        assert result.converted_amount == Decimal("100.00")
        assert result.rate_used == Decimal(1)
        assert result.source == "identity"

    async def test_convert_no_rate(self, service) -> None:
        """convert returns None when no rate is available."""
        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("100.00"),
        )
        result = await service.convert(request)
        assert result is None

    async def test_convert_with_local_rate(self, service, mock_uow) -> None:
        """convert uses local rate to perform conversion."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("100.00"),
        )
        result = await service.convert(request)
        assert result is not None
        assert result.converted_amount == Decimal("109.45")
        assert result.rate_used == Decimal("1.0945")
        assert result.original_amount == Decimal("100.00")

    async def test_get_rates_for_base(self, service, mock_uow) -> None:
        """get_rates_for_base returns rates for requested targets."""
        mock_usd = MagicMock()
        mock_usd.base_currency = "EUR"
        mock_usd.quote_currency = "USD"
        mock_usd.rate = Decimal("1.0945")
        mock_usd.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_usd.source = "openbb"

        mock_gbp = MagicMock()
        mock_gbp.base_currency = "EUR"
        mock_gbp.quote_currency = "GBP"
        mock_gbp.rate = Decimal("0.8600")
        mock_gbp.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_gbp.source = "openbb"

        # Return sequentially: EUR/USD then EUR/GBP
        mock_uow.fx_rates.list = AsyncMock()
        mock_uow.fx_rates.list.side_effect = [[mock_usd], [mock_gbp]]

        result = await service.get_rates_for_base(
            "EUR",
            targets=["USD", "GBP"],
        )
        assert "USD" in result
        assert result["USD"] == Decimal("1.0945")
        assert "GBP" in result
        assert result["GBP"] == Decimal("0.8600")

    async def test_get_rates_for_base_same_currency(self, service) -> None:
        """get_rates_for_base returns 1 for same currency target."""
        result = await service.get_rates_for_base(
            "EUR",
            targets=["EUR"],
        )
        assert result["EUR"] == Decimal(1)

    async def test_canonicalise_pair_major(self) -> None:
        """EUR/USD is stored as-is (major pair)."""
        base, quote, inverted = FxService._canonicalise_pair("EUR", "USD")
        assert base == "EUR"
        assert quote == "USD"
        assert not inverted

    async def test_canonicalise_pair_reversed_major(self) -> None:
        """USD/EUR is reversed to EUR/USD."""
        base, quote, inverted = FxService._canonicalise_pair("USD", "EUR")
        assert base == "EUR"
        assert quote == "USD"
        assert inverted

    async def test_canonicalise_pair_unknown(self) -> None:
        """Unknown pair stays in original order."""
        base, quote, inverted = FxService._canonicalise_pair("XRP", "BTC")
        assert base == "XRP"
        assert quote == "BTC"
        assert not inverted

    async def test_close_idempotent(self, service) -> None:
        """Calling close multiple times is safe."""
        await service.close()  # no-op, no client
        await service.close()  # idempotent


class TestFxRateObservation:
    """Tests for the FxRateObservation DTO."""

    def test_inverse(self) -> None:
        """inverse() returns the inverse rate observation."""
        obs = FxRateObservation(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        inv = obs.inverse()
        assert inv.base_currency == "USD"
        assert inv.quote_currency == "EUR"
        expected = round(Decimal(1) / Decimal("1.0945"), 12)
        assert inv.rate == expected
        assert inv.timestamp == obs.timestamp
        assert inv.source == obs.source


class TestFxConversionRequest:
    """Tests for the FxConversionRequest/Result DTOs."""

    def test_create_request(self) -> None:
        """Can create a conversion request with all fields."""
        req = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("1000.00"),
        )
        assert req.from_currency == "EUR"
        assert req.to_currency == "USD"
        assert req.amount == Decimal("1000.00")
        assert req.at_timestamp is None

    def test_create_request_with_timestamp(self) -> None:
        """Can create a conversion request with optional timestamp."""
        ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        req = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("1000.00"),
            at_timestamp=ts,
        )
        assert req.at_timestamp == ts
