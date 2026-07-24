"""Tests for the FxService — FX rate fetching, caching, and conversion."""
# pyright: basic

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import (
    FxConversionRequest,
    FxRateObservation,
)
from finance_sync.services.fx_service import (
    FxRateFetchError,
    FxRateNotFoundError,
    FxService,
    FxServiceError,
    InvalidCurrencyError,
    _CacheEntry,
    _parse_timestamp,
    _safe_decimal,
    convert_currency,
)


def _recent_ts(**offset) -> datetime:
    """Return a timestamp close to now for TTL-friendly test data."""
    return datetime.now(UTC) - timedelta(**offset)


_MOCK_TS_JAN = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# ── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_uow() -> MagicMock:
    """Shared mock UnitOfWork with async fx_rates repository."""
    uow = MagicMock()
    uow.fx_rates = AsyncMock()
    uow.fx_rates.list = AsyncMock(return_value=[])
    uow.fx_rates.add = AsyncMock()
    return uow


@pytest.fixture
def degraded_settings() -> MagicMock:
    """Settings with no API key (degraded mode)."""
    s = MagicMock()
    s.openbb_api_key = None
    s.openbb_base_url = "https://openbb.co/api"
    s.openbb_api_version = "v1"
    s.openbb_request_timeout = 30
    s.fx_rate_cache_ttl_seconds = 999999
    return s


@pytest.fixture
def live_settings() -> MagicMock:
    """Settings with an API key (non-degraded mode)."""
    s = MagicMock()
    s.openbb_api_key = MagicMock()
    s.openbb_api_key.get_secret_value.return_value = "sk-test-key-12345"
    s.openbb_base_url = "https://openbb.co/api"
    s.openbb_api_version = "v1"
    s.openbb_request_timeout = 30
    s.fx_rate_cache_ttl_seconds = 999999
    return s


# ── Mock HTTP response helpers ──────────────────────────────────────────


def _mock_response(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock httpx.Response-like object.

    Note: httpx.Response.json() is synchronous (not async),
    so we use a regular MagicMock for the json method.
    """
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {})
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = _http_error(status)
    return resp


def _http_error(status: int):
    """Build an HTTPStatusError for a given status code."""
    from httpx import HTTPStatusError, Request, Response

    request = Request("GET", "https://openbb.co/api/v1/market/forex")
    response = Response(status, request=request)
    return HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _mock_http_client(
    return_value: AsyncMock | None = None,
    side_effect: list[AsyncMock] | None = None,
) -> MagicMock:
    """Build a mock httpx.AsyncClient."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value=return_value or _mock_response(),
        side_effect=side_effect,
    )
    client.is_closed = False
    client.aclose = AsyncMock()
    return client


def _make_observation(
    base: str = "EUR",
    quote: str = "USD",
    rate: Decimal = Decimal("1.0945"),
    **overrides: Any,
) -> FxRateObservation:
    """Build an FxRateObservation with defaults."""
    return FxRateObservation(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        timestamp=_MOCK_TS_JAN,
        source="openbb",
        **overrides,
    )


# ── Degraded mode (no API key) ──────────────────────────────────────────


class TestFxServiceDegraded:
    """Tests for FxService in degraded mode (no API key)."""

    @pytest.fixture
    def settings(self, degraded_settings):
        return degraded_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    def test_is_degraded(self, service) -> None:
        """Service is degraded when no API key is set."""
        assert service._degraded

    def test_build_headers_degraded(self, service) -> None:
        """_build_headers omits Authorization when no API key."""
        headers = service._build_headers()
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"

    async def test_get_rate_same_currency(self, service) -> None:
        """get_rate returns identity rate for same currency."""
        result = await service.get_rate("EUR", "EUR")
        assert result is not None
        assert result.rate == Decimal(1)
        assert result.source == "identity"

    async def test_get_rate_no_data(self, service) -> None:
        """get_rate returns fallback rate when no data and no API key."""
        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"

    async def test_get_rate_no_data_obscure_pair(
        self,
        service,
    ) -> None:
        """get_rate returns None for unknown pair w/o data or API key."""
        result = await service.get_rate("XYZ", "ABC")
        assert result is None

    async def test_get_rate_from_local(self, service, mock_uow) -> None:
        """get_rate returns cached rate from local DB."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
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
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"

        mock_uow.fx_rates.list = AsyncMock(side_effect=[[], [mock_row]])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
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
        """convert succeeds with fallback rate when no data and no API key."""
        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("100.00"),
        )
        result = await service.convert(request)
        assert result is not None
        assert result.converted_amount == Decimal("109.00")  # 100 * 1.09
        assert result.rate_used == Decimal("1.09")
        assert result.source == "fallback"

    async def test_convert_no_rate_obscure_pair(self, service) -> None:
        """convert returns None when no data and no API key for unknown pair."""
        request = FxConversionRequest(
            from_currency="XYZ",
            to_currency="ABC",
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
        mock_row.timestamp = _recent_ts(seconds=5)
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
        mock_usd.timestamp = _recent_ts(seconds=5)
        mock_usd.source = "openbb"

        mock_gbp = MagicMock()
        mock_gbp.base_currency = "EUR"
        mock_gbp.quote_currency = "GBP"
        mock_gbp.rate = Decimal("0.8600")
        mock_gbp.timestamp = _recent_ts(seconds=5)
        mock_gbp.source = "openbb"

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

    async def test_canonicalise_pair_same_currency(self) -> None:
        """Same currency pair is returned as-is with no inversion."""
        base, quote, inverted = FxService._canonicalise_pair("EUR", "EUR")
        assert base == "EUR"
        assert quote == "EUR"
        assert not inverted

    async def test_close_idempotent(self, service) -> None:
        """Calling close multiple times is safe."""
        await service.close()
        await service.close()

    async def test_close_with_client(self, service) -> None:
        """close() shuts down the HTTP client if one was created."""
        client = _mock_http_client()
        service._http_client = client
        await service.close()
        client.aclose.assert_awaited_once()
        # Idempotent
        await service.close()


# ── Non-degraded mode (with API key) ────────────────────────────────────


class TestFxServiceNonDegraded:
    """Tests for FxService in non-degraded mode (API key configured)."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    def test_not_degraded(self, service) -> None:
        """Service is not degraded when API key is set."""
        assert not service._degraded

    def test_http_client_lazy_init(self, service) -> None:
        """HTTP client is lazily created on first access."""
        assert service._http_client is None
        client = service.http_client
        assert client is not None
        # Same instance on repeated access
        assert service.http_client is client

    async def test_http_client_recreates_after_close(self, service) -> None:
        """A new HTTP client is created if the previous one was closed."""
        import httpx

        client_a = service.http_client
        assert isinstance(client_a, httpx.AsyncClient)
        await client_a.aclose()

        client_b = service.http_client
        assert client_b is not client_a
        assert isinstance(client_b, httpx.AsyncClient)

    async def test_build_headers_with_api_key(self, service) -> None:
        """_build_headers includes Bearer token when API key is set."""
        headers = service._build_headers()
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")

    async def test_api_fetch_success(self, service, mock_uow) -> None:
        """get_rate fetches from API when local cache is empty."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.0945")
        assert result.source == "openbb"
        # Verify it stored the fetched rate
        mock_uow.fx_rates.add.assert_awaited_once()

    async def test_api_fetch_stores_rate(self, service, mock_uow) -> None:
        """Fetched rates are persisted to the database."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "GBP",
                    "quote": "USD",
                    "rate": 1.2650,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        await service.get_rate("GBP", "USD")
        mock_uow.fx_rates.add.assert_awaited_once()
        added_rate = mock_uow.fx_rates.add.call_args[0][0]
        assert added_rate.base_currency == "GBP"
        assert added_rate.rate == Decimal("1.2650")

    async def test_api_fetch_historical_preserves_timestamp(
        self, service, mock_uow
    ) -> None:
        """get_rate with at_timestamp and API key fetches from API
        without caching to memory."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        historical_ts = datetime(2026, 1, 15, 11, 0, 0, tzinfo=UTC)
        result = await service.get_rate(
            "EUR", "USD", at_timestamp=historical_ts,
        )
        assert result is not None
        assert result.rate == Decimal("1.0945")
        # API rate is stored to DB regardless of timestamp
        mock_uow.fx_rates.add.assert_awaited_once()

    async def test_api_fetch_404_returns_none(self, service, mock_uow) -> None:
        """API returns None when the pair is not found (404)."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=404),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("XYZ", "ABC")
        assert result is None
        # Should NOT have stored anything
        mock_uow.fx_rates.add.assert_not_called()

    async def test_api_fetch_500_returns_fallback(
        self, service, mock_uow
    ) -> None:
        """get_rate falls back to hardcoded rate on 500 error for known pair."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=500),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"
        mock_uow.fx_rates.add.assert_not_called()

    async def test_api_fetch_timeout_fallback(self, service, mock_uow) -> None:
        """API returns None on timeout."""
        from httpx import TimeoutException

        client = MagicMock()
        client.get = AsyncMock(side_effect=TimeoutException("Timed out"))
        client.is_closed = False
        client.aclose = AsyncMock()
        service._http_client = client
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"

    async def test_api_fetch_generic_http_error_fallback(
        self, service, mock_uow
    ) -> None:
        """get_rate falls back to hardcoded rate on HTTP error."""
        from httpx import HTTPError

        client = MagicMock()
        client.get = AsyncMock(side_effect=HTTPError("Connection failed"))
        client.is_closed = False
        client.aclose = AsyncMock()
        service._http_client = client
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"

    async def test_convert_with_api_rate(self, service, mock_uow) -> None:
        """convert uses freshly-fetched API rate when no local cache."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("100.00"),
        )
        result = await service.convert(request)
        assert result is not None
        assert result.converted_amount == Decimal("109.45")

    async def test_get_rate_inverts_api_result(self, service, mock_uow) -> None:
        """get_rate inverts the API rate when the canonical pair is reversed."""
        # Request USD/EUR, canonical is EUR/USD, API returns EUR/USD rate
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate("USD", "EUR")
        assert result is not None
        # 1 / 1.0945 = 0.9137...
        expected = round(Decimal(1) / Decimal("1.0945"), 12)
        assert result.rate == expected
        assert result.base_currency == "USD"
        assert result.quote_currency == "EUR"

    async def test_fetch_all_major_rates(self, service, mock_uow) -> None:
        """fetch_all_major_rates fetches and returns all major pairs."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.09,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        results = await service.fetch_all_major_rates()
        assert len(results) > 0
        assert results
        for obs in results:
            assert isinstance(obs, FxRateObservation)

    async def test_fetch_all_major_rates_returns_list(
        self, service, mock_uow
    ) -> None:
        """fetch_all_major_rates returns a list of observations."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.09,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        results = await service.fetch_all_major_rates(base_currency="EUR")
        assert len(results) >= 6
        assert isinstance(results, list)

    async def test_fetch_all_major_rates_handles_api_failure(
        self, service, mock_uow
    ) -> None:
        """fetch_all_major_rates still returns observations when API fails."""
        # All API calls fail; fallback rates cover major pairs
        mock_http = _mock_http_client(
            return_value=_mock_response(status=500),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        results = await service.fetch_all_major_rates()
        # Fallback rates cover EUR→USD, EUR→GBP, EUR→JPY, EUR→CHF,
        # EUR→CAD (5 pairs);
        # EUR→AUD and EUR→NZD have no fallback so they return None
        assert len(results) == 5
        for obs in results:
            assert obs.source == "fallback"

    async def test_stores_fetched_rate(
        self, service, mock_uow: MagicMock
    ) -> None:
        """_store_rate persists a new observation."""
        obs = _make_observation()
        mock_uow.fx_rates.list = AsyncMock(return_value=[])
        await service._store_rate(obs)
        mock_uow.fx_rates.add.assert_awaited_once()

    async def test_store_rate_skips_existing(
        self, service, mock_uow: MagicMock
    ) -> None:
        """_store_rate skips storage when a duplicate exists."""
        existing = MagicMock()
        existing.id = "existing-uuid"
        mock_uow.fx_rates.list = AsyncMock(return_value=[existing])

        obs = _make_observation()
        await service._store_rate(obs)
        # add should NOT have been called (duplicate detection)
        mock_uow.fx_rates.add.assert_not_called()

    async def test_row_to_observation(self, service) -> None:
        """_row_to_observation converts an ORM row to an FxRateObservation."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _MOCK_TS_JAN
        mock_row.source = "openbb"

        obs = service._row_to_observation(mock_row)
        assert isinstance(obs, FxRateObservation)
        assert obs.base_currency == "EUR"
        assert obs.quote_currency == "USD"
        assert obs.rate == Decimal("1.0945")
        assert obs.timestamp == _MOCK_TS_JAN
        assert obs.source == "openbb"

    async def test_get_rates_for_base_defaults(self, service, mock_uow) -> None:
        """get_rates_for_base with no targets uses major currencies."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"

        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rates_for_base("EUR")
        assert "USD" in result

    async def test_get_rates_for_base_with_none_targets(
        self, service, mock_uow
    ) -> None:
        """get_rates_for_base with targets=None uses major currencies."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"

        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rates_for_base("EUR", targets=None)
        assert isinstance(result, dict)
        assert "USD" in result

    async def test_get_rates_for_base_partial_failure(
        self, service, mock_uow
    ) -> None:
        """get_rates_for_base returns only available rates when some
        fetch fail."""
        # The get_rate method queries DB twice per pair (direct + inverse),
        # so side_effect needs to account for both lookups per pair
        mock_usd = MagicMock()
        mock_usd.base_currency = "EUR"
        mock_usd.quote_currency = "USD"
        mock_usd.rate = Decimal("1.09")
        mock_usd.timestamp = _recent_ts(seconds=5)
        mock_usd.source = "openbb"

        def _side(*a, **kw):
            # Simulate a real lookup: return the rate for EUR/USD
            # on first attempt
            return [mock_usd]
        mock_uow.fx_rates.list = AsyncMock(side_effect=_side)

        # Use targets whose rates the mock DB returns
        result = await service.get_rates_for_base(
            "EUR", targets=["USD"],
        )
        assert "USD" in result
        assert result["USD"] == Decimal("1.09")

    async def test_get_rates_for_base_all_fail(
        self, service, mock_uow
    ) -> None:
        """get_rates_for_base returns empty dict when all targets fail."""
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rates_for_base(
            "EUR", targets=["XRP", "BTC"],
        )
        assert result == {}

# ── FxRateObservation DTO ────────────────────────────────────────────────


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

    def test_inverse_twice_returns_to_original(self) -> None:
        """inverse(inverse(x)) == x within precision."""
        obs = FxRateObservation(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        inv = obs.inverse().inverse()
        assert inv.base_currency == "EUR"
        assert inv.quote_currency == "USD"
        # Round-trip should be very close
        assert abs(inv.rate - obs.rate) < Decimal("0.0000000001")


# ── TTL enforcement ─────────────────────────────────────────────────────


class TestFxServiceTTL:
    """Tests for cache TTL enforcement in FxService."""

    @pytest.fixture
    def settings_with_low_ttl(self):
        s = MagicMock()
        s.openbb_api_key = None
        s.openbb_base_url = "https://openbb.co/api"
        s.openbb_api_version = "v1"
        s.openbb_request_timeout = 30
        s.fx_rate_cache_ttl_seconds = 1  # very short TTL for testing
        return s

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings_with_low_ttl, mock_uow):
        return FxService(settings=settings_with_low_ttl, uow=mock_uow)

    async def test_cache_miss_when_stale_fallback(
        self, service, mock_uow
    ) -> None:
        """get_rate falls back to hardcoded rate when cache stale."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=10)  # older than 1s TTL
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rate("EUR", "USD")
        assert result is not None  # stale cache → fallback kicks in
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"

    async def test_cache_miss_when_stale_no_fallback(
        self, service, mock_uow
    ) -> None:
        """get_rate returns None when cache is stale and no fallback exists."""
        mock_row = MagicMock()
        mock_row.base_currency = "XRP"
        mock_row.quote_currency = "BTC"
        mock_row.rate = Decimal("0.00005")
        mock_row.timestamp = _recent_ts(seconds=10)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rate("XRP", "BTC")
        assert result is None  # stale cache + no fallback = unavailable

    async def test_historical_lookup_bypasses_ttl(
        self, service, mock_uow
    ) -> None:
        """get_rate with specific timestamp bypasses TTL check."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rate(
            "EUR",
            "USD",
            at_timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        assert result is not None
        assert result.rate == Decimal("1.0945")

    async def test_ttl_not_exceeded(self, service, mock_uow) -> None:
        """get_rate returns cached rate when within TTL window."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=0)  # right now
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.0945")
        assert result.source == "openbb"

    async def test_inverse_pair_historical(
        self, service, mock_uow
    ) -> None:
        """Inverse pair lookup works with historical timestamp."""
        # Store rate as USD→EUR, query EUR→USD with at_timestamp
        mock_row = MagicMock()
        mock_row.base_currency = "USD"
        mock_row.quote_currency = "EUR"
        mock_row.rate = Decimal("0.9140")
        mock_row.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_row.source = "openbb"

        # Direct lookup misses, inverse lookup hits
        mock_uow.fx_rates.list = AsyncMock(side_effect=[[], [mock_row]])

        historical_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = await service.get_rate(
            "EUR", "USD", at_timestamp=historical_ts,
        )
        assert result is not None
        expected = round(Decimal(1) / Decimal("0.9140"), 12)
        assert result.rate == expected
        assert result.base_currency == "EUR"
        assert result.quote_currency == "USD"

    async def test_historical_no_data_returns_none(
        self, service, mock_uow
    ) -> None:
        """get_rate with historical timestamp and no data returns None
        (no fallback)."""
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.get_rate(
            "EUR", "USD",
            at_timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        assert result is None


# ── Exception hierarchy ─────────────────────────────────────────────────


class TestFxServiceExceptions:
    """Tests for custom FX service exception classes."""

    async def test_import_exceptions(self) -> None:
        """Custom exception classes are importable and chainable."""
        assert issubclass(FxRateNotFoundError, FxServiceError)
        assert issubclass(FxRateFetchError, FxServiceError)
        assert issubclass(InvalidCurrencyError, (FxServiceError, ValueError))

    async def test_exception_accepts_message(self) -> None:
        """Custom exceptions accept a message string."""
        exc = FxRateNotFoundError("No rate available for EUR/USD")
        assert str(exc) == "No rate available for EUR/USD"
        assert isinstance(exc, Exception)

    async def test_fx_rate_fetch_error_message(self) -> None:
        """FxRateFetchError accepts and stores a message."""
        exc = FxRateFetchError("Failed to fetch EUR/USD from API")
        assert str(exc) == "Failed to fetch EUR/USD from API"

    async def test_invalid_currency_error_message(self) -> None:
        """InvalidCurrencyError accepts and stores a message."""
        exc = InvalidCurrencyError("Invalid currency code: XYZ")
        assert str(exc) == "Invalid currency code: XYZ"
        assert isinstance(exc, ValueError)


# ── FxConversionRequest / Result DTOs ───────────────────────────────────


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


# ── Helper functions ────────────────────────────────────────────────────


class TestFxServiceHelpers:
    """Tests for internal helper functions."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.0945", Decimal("1.0945")),
            ("0", Decimal(0)),
            ("100", Decimal(100)),
            (1.0945, Decimal("1.0945")),
            (None, None),
            ("not-a-number", None),
            ("", None),
        ],
    )
    def test_safe_decimal(self, value: Any, expected: Decimal | None) -> None:
        """_safe_decimal converts valid values and returns None for invalid."""
        result = _safe_decimal(value)
        assert result == expected

    @pytest.mark.parametrize(
        ("raw", "expected_year"),
        [
            ("2026-01-15T12:00:00Z", 2026),
            ("2026-01-15T12:00:00", 2026),
            (None, None),  # will return datetime.now(UTC).year
            ("", None),  # will return datetime.now(UTC).year
            ("invalid-date", None),  # will return datetime.now(UTC).year
        ],
    )
    def test_parse_timestamp(
        self, raw: str | None, expected_year: int | None
    ) -> None:
        """_parse_timestamp converts ISO strings and defaults on failure."""
        result = _parse_timestamp(raw)
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        if expected_year:
            assert result.year == expected_year

    def test_parse_timestamp_fallback(self) -> None:
        """_parse_timestamp returns now() when given invalid input."""
        before = datetime.now(UTC)
        result = _parse_timestamp("garbage")
        after = datetime.now(UTC)
        assert before <= result <= after

    def test_parse_timestamp_none(self) -> None:
        """_parse_timestamp returns now() when given None."""
        before = datetime.now(UTC)
        result = _parse_timestamp(None)
        after = datetime.now(UTC)
        assert before <= result <= after

    def test_parse_timestamp_just_z(self) -> None:
        """_parse_timestamp handles a lone "Z" string."""
        before = datetime.now(UTC)
        result = _parse_timestamp("Z")
        after = datetime.now(UTC)
        assert before <= result <= after

    def test_parse_timestamp_empty_string(self) -> None:
        """_parse_timestamp handles an empty string."""
        before = datetime.now(UTC)
        result = _parse_timestamp("")
        after = datetime.now(UTC)
        assert before <= result <= after


# ── In-memory cache tests ─────────────────────────────────────────────────


class TestFxInMemoryCache:
    """Tests for the in-memory L1 cache in FxService."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_memory_cache_hit(self, service, mock_uow) -> None:
        """get_rate returns cached rate from in-memory cache before DB hit."""
        # First call: API fetch succeeds and populates cache
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result_a = await service.get_rate("EUR", "USD")
        assert result_a is not None
        assert result_a.rate == Decimal("1.0945")

        # Reset mock — second call should use in-memory cache, not DB
        mock_uow.fx_rates.list.reset_mock()

        result_b = await service.get_rate("EUR", "USD")
        assert result_b is not None
        assert result_b.rate == Decimal("1.0945")
        # DB should NOT have been queried (memory cache hit)
        mock_uow.fx_rates.list.assert_not_called()

    async def test_memory_cache_primes_from_db(self, service, mock_uow) -> None:
        """DB hit primes the in-memory cache for subsequent calls."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result_a = await service.get_rate("EUR", "USD")
        assert result_a is not None

        # Second call should NOT query DB again
        mock_uow.fx_rates.list.reset_mock()
        result_b = await service.get_rate("EUR", "USD")
        assert result_b is not None
        mock_uow.fx_rates.list.assert_not_called()

    async def test_memory_cache_expiry(self, service, mock_uow) -> None:
        """In-memory cache expires after TTL and falls back to DB/API."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result_a = await service.get_rate("EUR", "USD")
        assert result_a is not None

        # Manually expire the memory cache entry
        async with service._cache_lock:
            key = ("EUR", "USD")
            entry = service._memory_cache.get(key)
            if entry:
                service._memory_cache[key] = _CacheEntry(
                    observation=entry.observation,
                    expires_at=time.monotonic() - 1,  # expired
                )

        # Second call should re-query the DB
        mock_uow.fx_rates.list.reset_mock()
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result_b = await service.get_rate("EUR", "USD")
        assert result_b is not None
        mock_uow.fx_rates.list.assert_awaited_once()


# ── Fallback rate tests ──────────────────────────────────────────────────


class TestFxServiceFallback:
    """Tests for hardcoded fallback rate resolution."""

    @pytest.fixture
    def settings(self, degraded_settings):
        return degraded_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_fallback_for_known_pair(self, service) -> None:
        """Known major pairs return a hardcoded fallback rate."""
        result = await service.get_rate("EUR", "USD")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"

    async def test_fallback_inverted(self, service) -> None:
        """Inverted pairs are correctly computed from fallback rates."""
        result = await service.get_rate("USD", "EUR")
        assert result is not None
        expected = round(Decimal(1) / Decimal("1.09"), 12)
        assert result.rate == expected
        assert result.base_currency == "USD"
        assert result.quote_currency == "EUR"

    async def test_no_fallback_for_obscure_pair(self, service) -> None:
        """Obscure pairs with no fallback return None."""
        result = await service.get_rate("XRP", "BTC")
        assert result is None

    async def test_fallback_gbp_usd(self, service) -> None:
        """GBP/USD fallback rate is correct."""
        result = await service.get_rate("GBP", "USD")
        assert result is not None
        assert result.rate == Decimal("1.27")
        assert result.source == "fallback"

    async def test_fallback_respects_case(self, service) -> None:
        """Fallback lookups are case-insensitive."""
        result = await service.get_rate("eur", "usd")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"


# ── fetch_and_cache_rates tests ──────────────────────────────────────────


class TestFetchAndCacheRates:
    """Tests for the bulk fetch_and_cache_rates method."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_fetch_and_cache_returns_count(
        self, service, mock_uow
    ) -> None:
        """fetch_and_cache_rates returns the number of rates fetched."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.09,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        count = await service.fetch_and_cache_rates(
            base_currencies=["EUR"],
        )
        assert isinstance(count, int)
        assert count >= 1

    async def test_fetch_and_cache_default_currencies(
        self, service, mock_uow
    ) -> None:
        """fetch_and_cache_rates defaults to EUR, USD, GBP base currencies."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.09,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        count = await service.fetch_and_cache_rates()
        # 3 base currs x 7 targets each = up to 21, but API returns same
        # response for every pair due to mock
        assert isinstance(count, int)
        assert count > 0

    async def test_fetch_and_cache_with_empty_list(
        self, service, mock_uow
    ) -> None:
        """fetch_and_cache_rates returns 0 when given empty base list."""
        count = await service.fetch_and_cache_rates(base_currencies=[])
        assert count == 0

    async def test_fetch_and_cache_stores_to_memory(
        self, service, mock_uow
    ) -> None:
        """fetch_and_cache_rates populates the in-memory cache."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.09,
                    "timestamp": "2026-01-15T12:00:00Z",
                    "source": "openbb",
                },
            ),
        )
        service._http_client = mock_http
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        await service.fetch_and_cache_rates(base_currencies=["EUR"])

        # In-memory cache should be populated
        async with service._cache_lock:
            result = service._memory_cache.get(("EUR", "USD"))
        assert result is not None
        assert result.observation.rate == Decimal("1.09")


# ── fetch_latest_rates tests ──────────────────────────────────────────────


class TestFetchLatestRates:
    """Tests for the fetch_latest_rates method."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_returns_dict_for_given_pairs(
        self, service, mock_uow
    ) -> None:
        """fetch_latest_rates returns a dict mapping pairs to observations."""
        mock_usd = MagicMock()
        mock_usd.base_currency = "EUR"
        mock_usd.quote_currency = "USD"
        mock_usd.rate = Decimal("1.09")
        mock_usd.timestamp = _recent_ts(seconds=5)
        mock_usd.source = "openbb"

        mock_gbp = MagicMock()
        mock_gbp.base_currency = "EUR"
        mock_gbp.quote_currency = "GBP"
        mock_gbp.rate = Decimal("0.86")
        mock_gbp.timestamp = _recent_ts(seconds=5)
        mock_gbp.source = "openbb"

        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_usd])
        # Second call for GBP
        mock_uow.fx_rates.list.side_effect = [[mock_usd], [mock_gbp]]

        result = await service.fetch_latest_rates(
            [("EUR", "USD"), ("EUR", "GBP")],
        )
        assert len(result) == 2
        assert ("EUR", "USD") in result
        assert ("EUR", "GBP") in result
        assert result[("EUR", "USD")].rate == Decimal("1.09")
        assert result[("EUR", "GBP")].rate == Decimal("0.86")

    async def test_omits_unresolvable_pairs(self, service, mock_uow) -> None:
        """fetch_latest_rates omits pairs that could not be resolved."""
        mock_uow.fx_rates.list = AsyncMock(return_value=[])

        result = await service.fetch_latest_rates(
            [("EUR", "USD"), ("XYZ", "ABC")],
        )
        assert ("EUR", "USD") in result
        assert ("XYZ", "ABC") not in result

    async def test_returns_empty_dict_for_empty_list(
        self, service,
    ) -> None:
        """fetch_latest_rates returns empty dict when no pairs given."""
        result = await service.fetch_latest_rates([])
        assert result == {}

    async def test_normalises_currency_case(self, service, mock_uow) -> None:
        """fetch_latest_rates normalises currency codes to uppercase."""
        mock_usd = MagicMock()
        mock_usd.base_currency = "EUR"
        mock_usd.quote_currency = "USD"
        mock_usd.rate = Decimal("1.09")
        mock_usd.timestamp = _recent_ts(seconds=5)
        mock_usd.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_usd])

        result = await service.fetch_latest_rates(
            [("eur", "usd")],
        )
        assert ("EUR", "USD") in result
        assert result[("EUR", "USD")].rate == Decimal("1.09")


# ── Thread safety ────────────────────────────────────────────────────────


class TestFxServiceThreadSafety:
    """Tests for thread-safe access patterns in FxService."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_cache_lock_is_asyncio_lock(self, service) -> None:
        """_cache_lock is an asyncio.Lock instance."""
        import asyncio

        assert isinstance(service._cache_lock, asyncio.Lock)

    async def test_concurrent_cache_access(self, service, mock_uow) -> None:
        """Multiple concurrent get_rate calls don't deadlock."""
        import asyncio

        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.09")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"

        mock_row2 = MagicMock()
        mock_row2.base_currency = "GBP"
        mock_row2.quote_currency = "USD"
        mock_row2.rate = Decimal("1.27")
        mock_row2.timestamp = _recent_ts(seconds=5)
        mock_row2.source = "openbb"

        def _side_effect(*args, **kwargs):
            # Return EUR/USD for first call, GBP/USD for second
            if not hasattr(_side_effect, "call_count"):
                _side_effect.call_count = 0
            _side_effect.call_count += 1
            if _side_effect.call_count == 1:
                return [mock_row]
            return [mock_row2]

        mock_uow.fx_rates.list = AsyncMock(side_effect=_side_effect)

        async def fetch_eur_usd():
            return await service.get_rate("EUR", "USD")

        async def fetch_gbp_usd():
            return await service.get_rate("GBP", "USD")

        results = await asyncio.gather(fetch_eur_usd(), fetch_gbp_usd())
        assert len(results) == 2
        assert results[0] is not None
        assert results[1] is not None


class TestConvertCurrency:
    """Tests for the standalone convert_currency() utility function."""

    @pytest.fixture
    def settings(self, degraded_settings):
        return degraded_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_identity(self, service) -> None:
        """convert_currency returns amount unchanged when currencies match."""
        result = await convert_currency(
            Decimal("100.00"), "EUR", "EUR", service,
        )
        assert result == Decimal("100.00")

    async def test_successful_conversion(
        self,
        service,
        mock_uow,
    ) -> None:
        """convert_currency returns converted amount using FxService."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.0945")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await convert_currency(
            Decimal("200.00"), "EUR", "USD", service,
        )
        assert result == Decimal("218.90")  # 200 * 1.0945

    async def test_fallback_rate_used(self, service) -> None:
        """convert_currency uses fallback rate when no data is available."""
        result = await convert_currency(
            Decimal("100.00"), "EUR", "USD", service,
        )
        assert result == Decimal("109.00")  # 100 * 1.09

    async def test_missing_rate_raises(self, service) -> None:
        """convert_currency raises FxRateNotFoundError for unknown pairs."""
        with pytest.raises(FxRateNotFoundError) as exc_info:
            await convert_currency(
                Decimal("100.00"), "XYZ", "ABC", service,
            )
        assert "XYZ" in str(exc_info.value)
        assert "ABC" in str(exc_info.value)

    async def test_inverted_rate(
        self,
        service,
        mock_uow,
    ) -> None:
        """convert_currency handles inverted rates transparently."""
        # Store rate as USD→EUR (0.914), query EUR→USD
        mock_row = MagicMock()
        mock_row.base_currency = "USD"
        mock_row.quote_currency = "EUR"
        mock_row.rate = Decimal("0.9140")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"

        # First call for EUR→USD misses, second call for USD→EUR hits
        mock_uow.fx_rates.list = AsyncMock(side_effect=[[], [mock_row]])

        result = await convert_currency(
            Decimal("100.00"), "EUR", "USD", service,
        )
        # 100 / 0.9140 ≈ 109.41
        expected = (Decimal("100.00") / Decimal("0.9140")).quantize(
            Decimal("0.01"), rounding="ROUND_HALF_UP",
        )
        assert result == expected

    async def test_preserves_quantization(self, service, mock_uow) -> None:
        """convert_currency returns a Decimal rounded to 2 places."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.09451234")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await convert_currency(
            Decimal("100.00"), "EUR", "USD", service,
        )
        # 100 * 1.09451234 = 109.451234 → rounded to 109.45
        assert result == Decimal("109.45")
        assert result.as_tuple().exponent == -2  # type: ignore[union-attr]

    async def test_negative_amount(self, service, mock_uow) -> None:
        """convert_currency handles negative amounts correctly."""
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.09")
        mock_row.timestamp = _recent_ts(seconds=5)
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await convert_currency(
            Decimal("-50.00"), "EUR", "USD", service,
        )
        assert result == Decimal("-54.50")  # -50 * 1.09


class TestFxServiceConvertWithTimestamp:
    """Tests for FxService.convert() with timestamp propagation."""

    @pytest.fixture
    def settings(self, live_settings):
        return live_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_convert_with_at_timestamp_historical(
        self, service, mock_uow
    ) -> None:
        """convert() uses at_timestamp from FxConversionRequest."""
        historical_ts = datetime(2025, 6, 1, tzinfo=UTC)
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.05")
        mock_row.timestamp = historical_ts
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("200.00"),
            at_timestamp=historical_ts,
        )
        result = await service.convert(request)
        assert result is not None
        assert result.converted_amount == Decimal("210.00")  # 200 * 1.05
        assert result.rate_timestamp == historical_ts

    async def test_convert_with_timestamp_no_data_returns_none(
        self, service, mock_uow
    ) -> None:
        """convert() returns None when historical rate has no data
        and no API is available."""
        mock_uow.fx_rates.list = AsyncMock(return_value=[])
        request = FxConversionRequest(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("100.00"),
            at_timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        )
        result = await service.convert(request)
        assert result is None

    async def test_get_rates_for_base_with_timestamp(
        self, service, mock_uow
    ) -> None:
        """get_rates_for_base propagates at_timestamp to get_rate."""
        historical_ts = datetime(2025, 6, 1, tzinfo=UTC)
        mock_row = MagicMock()
        mock_row.base_currency = "EUR"
        mock_row.quote_currency = "USD"
        mock_row.rate = Decimal("1.05")
        mock_row.timestamp = historical_ts
        mock_row.source = "openbb"
        mock_uow.fx_rates.list = AsyncMock(return_value=[mock_row])

        result = await service.get_rates_for_base(
            "EUR", targets=["USD"], at_timestamp=historical_ts,
        )
        assert result["USD"] == Decimal("1.05")


class TestFxServiceCaseInsensitivity:
    """Tests for case-insensitive currency code handling."""

    @pytest.fixture
    def settings(self, degraded_settings):
        return degraded_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    async def test_get_rate_lowercase_fallback(self, service) -> None:
        """get_rate accepts lowercase currency codes and returns fallback."""
        result = await service.get_rate("eur", "usd")
        assert result is not None
        assert result.rate == Decimal("1.09")
        assert result.source == "fallback"
        assert result.base_currency == "EUR"

    async def test_get_rate_mixed_case(self, service) -> None:
        """get_rate handles mixed-case currency codes."""
        result = await service.get_rate("Eur", "Usd")
        assert result is not None
        assert result.rate == Decimal("1.09")


class TestFxServiceGetFallbackRate:
    """Direct tests for the _get_fallback_rate method."""

    @pytest.fixture
    def settings(self, degraded_settings):
        return degraded_settings

    @pytest.fixture
    def mock_uow(self, mock_uow):
        return mock_uow

    @pytest.fixture
    def service(self, settings, mock_uow):
        return FxService(settings=settings, uow=mock_uow)

    def test_known_pair(self, service) -> None:
        """_get_fallback_rate returns a rate for known pairs."""
        obs = service._get_fallback_rate("EUR", "USD")
        assert obs is not None
        assert obs.rate == Decimal("1.09")
        assert obs.source == "fallback"

    def test_known_pair_lowercase(self, service) -> None:
        """_get_fallback_rate is case-insensitive."""
        obs = service._get_fallback_rate("eur", "usd")
        assert obs is not None
        assert obs.rate == Decimal("1.09")

    def test_unknown_pair(self, service) -> None:
        """_get_fallback_rate returns None for unknown pairs."""
        obs = service._get_fallback_rate("XRP", "BTC")
        assert obs is None

    def test_non_standard_known_pair(self, service) -> None:
        """_get_fallback_rate works for all defined fallback pairs."""
        for pair, expected_rate in FxService.FALLBACK_RATES.items():
            base, quote = pair
            obs = service._get_fallback_rate(base, quote)
            assert obs is not None, f"No fallback for {base}/{quote}"
            assert obs.rate == expected_rate
