"""Tests for OpenBBFxProvider — FX rate fetching, error handling, rate limiting."""
# pyright: basic

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from finance_sync.providers.openbb_fx import (
    OpenBBFxProvider,
    OpenBBFxProviderAuthError,
    OpenBBFxProviderError,
    OpenBBFxProviderInvalidResponseError,
    OpenBBFxProviderNotFoundError,
    OpenBBFxProviderRateLimitError,
    OpenBBFxProviderTimeoutError,
)

# ── Mock helpers ─────────────────────────────────────────────────────────────


def _mock_response(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {})
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = _http_error(status)
    return resp


def _http_error(status: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError for a given status code."""
    request = httpx.Request("GET", "https://openbb.co/api/v1/market/forex")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def _mock_http_client(
    return_value: MagicMock | None = None,
    side_effect: Any = None,
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


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def degraded_provider() -> OpenBBFxProvider:
    """Provider with no API key (degraded mode)."""
    return OpenBBFxProvider(api_key=None, max_requests_per_second=0)


@pytest.fixture
def live_provider() -> OpenBBFxProvider:
    """Provider with a fake API key (live mode)."""
    return OpenBBFxProvider(
        api_key="sk-test-key-12345",
        max_requests_per_second=0,  # disable rate limiting for tests
    )


@pytest.fixture
def live_provider_with_rate_limit() -> OpenBBFxProvider:
    """Provider with a fake API key and active rate limiting (1 req/s)."""
    return OpenBBFxProvider(
        api_key="sk-test-key-12345",
        max_requests_per_second=1,
    )


# ── Degraded mode (no API key) ───────────────────────────────────────────────


class TestDegradedMode:
    """Provider with no API key should refuse all requests."""

    async def test_get_latest_rate_raises_auth_error(
        self, degraded_provider: OpenBBFxProvider
    ) -> None:
        """get_latest_rate raises OpenBBFxProviderAuthError when no API key."""
        with pytest.raises(
            OpenBBFxProviderAuthError,
            match="API key not configured",
        ):
            await degraded_provider.get_latest_rate("EUR", "USD")

    async def test_degraded_flag(self, degraded_provider: OpenBBFxProvider) -> None:
        """_degraded is True when no API key is set."""
        assert degraded_provider._degraded

    async def test_close_idempotent(self, degraded_provider: OpenBBFxProvider) -> None:
        """close() is safe to call on degraded provider."""
        await degraded_provider.close()
        await degraded_provider.close()


# ── Successful fetch ─────────────────────────────────────────────────────────


class TestSuccessfulFetch:
    """Normal operation — API returns a valid rate."""

    async def test_get_latest_rate_returns_decimal(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """get_latest_rate returns a Decimal for a valid pair."""
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
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("EUR", "USD")
        assert isinstance(rate, Decimal)
        assert rate == Decimal("1.0945")

    async def test_get_latest_rate_uppercases_currencies(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Currency codes are uppercased before the API call."""
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
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("eur", "usd")
        assert rate == Decimal("1.2650")
        # Verify the request params were uppercased
        call_kwargs = mock_http.get.call_args[1]
        assert call_kwargs["params"]["base"] == "EUR"
        assert call_kwargs["params"]["quote"] == "USD"

    async def test_get_latest_rate_with_rate_string(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Rate returned as a string is handled correctly."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "JPY",
                    "rate": "162.96",
                    "source": "openbb",
                },
            ),
        )
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("EUR", "JPY")
        assert isinstance(rate, Decimal)
        assert rate == Decimal("162.96")

    async def test_get_latest_rate_http_client_lazy_init(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP client is lazily created on first API call."""
        assert live_provider._http_client is None
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={"rate": 1.0945},
            ),
        )
        live_provider._http_client = mock_http

        await live_provider.get_latest_rate("EUR", "USD")
        # Should use the provided client
        assert live_provider._http_client is mock_http

    async def test_get_latest_rate_strips_whitespace(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Whitespace around currency codes is stripped."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={"base": "EUR", "quote": "USD", "rate": 1.0945},
            ),
        )
        live_provider._http_client = mock_http

        await live_provider.get_latest_rate("  EUR ", "  USD ")
        call_kwargs = mock_http.get.call_args[1]
        assert call_kwargs["params"]["base"] == "EUR"
        assert call_kwargs["params"]["quote"] == "USD"


# ── Error handling ───────────────────────────────────────────────────────────


class TestErrorHandling:
    """Provider correctly maps HTTP errors to typed exceptions."""

    async def test_401_raises_auth_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP 401 raises OpenBBFxProviderAuthError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=401),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderAuthError, match="401"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_403_raises_auth_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP 403 raises OpenBBFxProviderAuthError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=403),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderAuthError, match="403"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_404_raises_not_found_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP 404 raises OpenBBFxProviderNotFoundError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=404),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderNotFoundError, match="404"):
            await live_provider.get_latest_rate("XYZ", "ABC")

    async def test_429_raises_rate_limit_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP 429 raises OpenBBFxProviderRateLimitError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=429),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderRateLimitError, match="rate limit"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_500_raises_generic_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """HTTP 500 raises OpenBBFxProviderError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=500),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderError, match="500"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_timeout_raises_timeout_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Request timeout raises OpenBBFxProviderTimeoutError."""
        mock_http = _mock_http_client(
            side_effect=httpx.TimeoutException("Connection timed out"),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderTimeoutError, match="timed out"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_network_error_raises_generic_error(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Network-level HTTPError raises OpenBBFxProviderError."""
        mock_http = _mock_http_client(
            side_effect=httpx.NetworkError("Connection refused"),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderError, match="Connection refused"):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_invalid_json_raises_invalid_response(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Non-JSON response raises OpenBBFxProviderInvalidResponseError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data=None,
            ),
        )
        # Make json() raise ValueError
        mock_http.get.return_value.json.side_effect = ValueError(
            "Expecting value"
        )
        live_provider._http_client = mock_http

        with pytest.raises(
            OpenBBFxProviderInvalidResponseError, match="Invalid JSON"
        ):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_missing_rate_field_raises_invalid_response(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Response without 'rate' field raises OpenBBFxProviderInvalidResponseError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "timestamp": "2026-01-15T12:00:00Z",
                    # no "rate" key
                },
            ),
        )
        live_provider._http_client = mock_http

        with pytest.raises(
            OpenBBFxProviderInvalidResponseError,
            match="missing 'rate'",
        ):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_null_rate_raises_invalid_response(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Response with null rate raises OpenBBFxProviderInvalidResponseError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": None,
                },
            ),
        )
        live_provider._http_client = mock_http

        with pytest.raises(
            OpenBBFxProviderInvalidResponseError,
            match="missing 'rate'",
        ):
            await live_provider.get_latest_rate("EUR", "USD")

    async def test_non_numeric_rate_raises_invalid_response(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Non-numeric rate value raises OpenBBFxProviderInvalidResponseError."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": "NON_NUMERIC",
                },
            ),
        )
        live_provider._http_client = mock_http

        with pytest.raises(
            OpenBBFxProviderInvalidResponseError,
            match="Invalid rate value",
        ):
            await live_provider.get_latest_rate("EUR", "USD")


# ── Rate limiting ────────────────────────────────────────────────────────────


class TestRateLimiting:
    """Sliding-window rate limiting works as expected."""

    async def test_rate_limiter_blocks_excess_requests(
        self, live_provider_with_rate_limit: OpenBBFxProvider
    ) -> None:
        """Rate limiter delays requests that exceed the configured limit.

        With 1 req/s and issuing 2 requests back-to-back, the second
        call should take at least ~1 second.
        """
        provider = live_provider_with_rate_limit
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "EUR",
                    "quote": "USD",
                    "rate": 1.0945,
                },
            ),
        )
        provider._http_client = mock_http

        # First call should be instant
        t0 = asyncio.get_event_loop().time()
        await provider.get_latest_rate("EUR", "USD")
        t1 = asyncio.get_event_loop().time()

        # Second call should be throttled (~1s delay)
        await provider.get_latest_rate("EUR", "USD")
        t2 = asyncio.get_event_loop().time()

        # First call took < 0.5s (no delay)
        assert (t1 - t0) < 0.5
        # Second call took >= 0.8s (throttled — with some slack for CI)
        assert (t2 - t1) >= 0.8

    async def test_rate_limiter_disabled_with_zero(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Setting max_requests_per_second=0 disables rate limiting."""
        provider = OpenBBFxProvider(
            api_key="sk-test-key",
            max_requests_per_second=0,
        )
        assert provider._rate_limit_max == 0

        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={"rate": 1.0945},
            ),
        )
        provider._http_client = mock_http

        t0 = asyncio.get_event_loop().time()
        for _ in range(5):
            await provider.get_latest_rate("EUR", "USD")
        t1 = asyncio.get_event_loop().time()

        # All 5 calls should complete in well under 0.5s (no throttling)
        assert (t1 - t0) < 0.5

    async def test_rate_limiter_handles_negative_value(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Negative max_requests_per_second is clamped to 0 (disabled)."""
        provider = OpenBBFxProvider(
            api_key="sk-test-key",
            max_requests_per_second=-1,
        )
        assert provider._rate_limit_max == 0


# ── HTTP client lifecycle ────────────────────────────────────────────────────


class TestHTTPClientLifecycle:
    """HTTP client creation, reuse, and teardown."""

    async def test_http_client_property_lazy_init(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """http_client property creates the client on first access."""
        assert live_provider._http_client is None
        client = live_provider.http_client
        assert isinstance(client, httpx.AsyncClient)
        assert live_provider._http_client is client

    async def test_http_client_caching(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Repeated access returns the same client instance."""
        client_a = live_provider.http_client
        client_b = live_provider.http_client
        assert client_a is client_b

    async def test_http_client_recreates_after_close(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """A closed HTTP client is replaced on next access."""
        client_a = live_provider.http_client
        await client_a.aclose()
        client_b = live_provider.http_client
        assert client_b is not client_a
        assert isinstance(client_b, httpx.AsyncClient)

    async def test_close_idempotent_with_client(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """close() is safe to call multiple times."""
        mock_client = _mock_http_client(
            return_value=_mock_response(json_data={"rate": 1.0}),
        )
        live_provider._http_client = mock_client
        await live_provider.close()
        mock_client.aclose.assert_awaited_once()
        await live_provider.close()  # second call — no error

    async def test_close_no_client(self, live_provider: OpenBBFxProvider) -> None:
        """close() is safe when no client was created."""
        assert live_provider._http_client is None
        await live_provider.close()  # should not raise


# ── Header building ──────────────────────────────────────────────────────────


class TestHeaders:
    """Provider builds correct HTTP headers."""

    def test_build_headers_with_api_key(self) -> None:
        """_build_headers includes Bearer token when API key is set."""
        provider = OpenBBFxProvider(api_key="sk-test-key")
        headers = provider._build_headers()
        assert headers["Authorization"] == "Bearer sk-test-key"
        assert headers["Accept"] == "application/json"

    def test_build_headers_without_api_key(self) -> None:
        """_build_headers omits Authorization when no API key."""
        provider = OpenBBFxProvider(api_key=None)
        headers = provider._build_headers()
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Boundary conditions and unusual inputs."""

    async def test_empty_currency_code(self, live_provider: OpenBBFxProvider) -> None:
        """Empty currency codes are passed through (API handles validation)."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={"rate": 1.0},
            ),
        )
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("", "")
        assert rate == Decimal("1.0")

    async def test_special_characters_in_currency(
        self, live_provider: OpenBBFxProvider
    ) -> None:
        """Special characters are passed through (API handles validation)."""
        mock_http = _mock_http_client(
            return_value=_mock_response(status=404),
        )
        live_provider._http_client = mock_http

        with pytest.raises(OpenBBFxProviderNotFoundError):
            await live_provider.get_latest_rate("$%^", "&*(")

    async def test_base_url_trailing_slash_stripped(self) -> None:
        """Trailing slash on base_url is stripped."""
        provider = OpenBBFxProvider(
            api_key="sk-test",
            base_url="https://custom.example.com/api/",
        )
        assert provider._base_url == "https://custom.example.com/api"

    async def test_default_base_url(self) -> None:
        """Default base URL is used when none is provided."""
        provider = OpenBBFxProvider(api_key="sk-test")
        assert provider._base_url == "https://openbb.co/api"

    async def test_very_small_rate(self, live_provider: OpenBBFxProvider) -> None:
        """Very small rates (e.g. JPY-related) are handled."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "JPY",
                    "quote": "EUR",
                    "rate": 0.0061,
                },
            ),
        )
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("JPY", "EUR")
        assert rate == Decimal("0.0061")

    async def test_large_rate_value(self, live_provider: OpenBBFxProvider) -> None:
        """Large rate values are handled correctly."""
        mock_http = _mock_http_client(
            return_value=_mock_response(
                json_data={
                    "base": "USD",
                    "quote": "IRR",
                    "rate": 42000.0,
                },
            ),
        )
        live_provider._http_client = mock_http

        rate = await live_provider.get_latest_rate("USD", "IRR")
        assert rate == Decimal(42000)


# ── Exception hierarchy ──────────────────────────────────────────────────────


class TestExceptionHierarchy:
    """All provider exceptions inherit from a common base."""

    def test_auth_error_is_provider_error(self) -> None:
        assert issubclass(OpenBBFxProviderAuthError, OpenBBFxProviderError)

    def test_rate_limit_error_is_provider_error(self) -> None:
        assert issubclass(OpenBBFxProviderRateLimitError, OpenBBFxProviderError)

    def test_not_found_error_is_provider_error(self) -> None:
        assert issubclass(OpenBBFxProviderNotFoundError, OpenBBFxProviderError)

    def test_timeout_error_is_provider_error(self) -> None:
        assert issubclass(OpenBBFxProviderTimeoutError, OpenBBFxProviderError)

    def test_invalid_response_error_is_provider_error(self) -> None:
        assert issubclass(
            OpenBBFxProviderInvalidResponseError, OpenBBFxProviderError
        )
