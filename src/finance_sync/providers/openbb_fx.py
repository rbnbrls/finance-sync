"""OpenBB FX provider — fetches exchange rates from the OpenBB Platform API.

Provides:
- :class:`OpenBBFxProvider` — a standalone data provider for FX rates
- `get_latest_rate(base, quote) -> Decimal` with error handling and
  rate limiting
- A clean exception hierarchy for all failure modes

Usage::

    provider = OpenBBFxProvider(api_key="sk-…")
    rate = await provider.get_latest_rate("EUR", "USD")
    # => Decimal('1.0945')
    await provider.close()
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ── Exception hierarchy ──────────────────────────────────────────────────────


class OpenBBFxProviderError(Exception):
    """Base exception for OpenBB FX provider errors."""


class OpenBBFxProviderAuthError(OpenBBFxProviderError):
    """Raised when authentication fails (invalid or missing API key)."""


class OpenBBFxProviderRateLimitError(OpenBBFxProviderError):
    """Raised when the provider rate limit is exceeded (HTTP 429)."""


class OpenBBFxProviderNotFoundError(OpenBBFxProviderError):
    """Raised when a currency pair is not found (HTTP 404)."""


class OpenBBFxProviderTimeoutError(OpenBBFxProviderError):
    """Raised when the API request times out."""


class OpenBBFxProviderInvalidResponseError(OpenBBFxProviderError):
    """Raised when the API returns an unexpected or malformed response."""


# ── Provider ─────────────────────────────────────────────────────────────────


class OpenBBFxProvider:
    """FX rate provider backed by the OpenBB Platform REST API.

    Fetches live exchange rates with authentication, error handling,
    and configurable rate limiting.

    .. code-block:: python

        provider = OpenBBFxProvider(
            api_key="sk-...",
            max_requests_per_second=10,
        )
        rate = await provider.get_latest_rate("EUR", "USD")
        print(f"EUR/USD = {rate}")

    Attributes:
        DEFAULT_BASE_URL: Default OpenBB API base URL.
    """

    DEFAULT_BASE_URL: str = "https://openbb.co/api"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        max_requests_per_second: int = 10,
        request_timeout: float = 30.0,
    ) -> None:
        """Initialise the provider.

        Args:
            api_key: OpenBB Platform API key.  When ``None`` the provider
                operates in *degraded* mode and every ``get_latest_rate``
                call raises :class:`OpenBBFxProviderAuthError`.
            base_url: Override the OpenBB API base URL.
            max_requests_per_second: Maximum requests per second allowed.
                Set to ``0`` to disable rate limiting entirely.
            request_timeout: HTTP request timeout in seconds.

        Raises:
            OpenBBFxProviderError: On construction failure.
        """
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._timeout = request_timeout

        # -- Sliding-window rate limiter state ---------------------------------
        self._rate_limit_max = max(0, max_requests_per_second)
        self._rate_limit_window = 1.0  # seconds
        self._request_timestamps: list[float] = []
        self._rate_lock = asyncio.Lock()

        # -- HTTP client (lazy) ------------------------------------------------
        self._http_client: httpx.AsyncClient | None = None

        self._degraded = api_key is None
        if self._degraded:
            logger.warning(
                "openbb_fx_provider_degraded",
                reason="no_api_key",
                message=(
                    "OpenBB API key not configured -- "
                    "call get_latest_rate will raise OpenBBFxProviderAuthError."
                ),
            )

    # -- Public API ------------------------------------------------------------

    async def get_latest_rate(self, base: str, quote: str) -> Decimal:
        """Fetch the latest exchange rate for a currency pair.

        Resolution order:
            1. Check if provider is degraded (no API key) → raise.
            2. Apply the sliding-window rate limiter.
            3. Issue an HTTP GET to the OpenBB ``/market/forex`` endpoint.
            4. Parse and validate the response.

        Args:
            base:  ISO-4217 base currency code (e.g. ``"EUR"``).
            quote: ISO-4217 quote currency code (e.g. ``"USD"``).

        Returns:
            The exchange rate as a :class:`Decimal`
            (1 *base* = *rate* x *quote*).

        Raises:
            OpenBBFxProviderAuthError:          No API key or auth failure.
            OpenBBFxProviderRateLimitError:     Rate limit exceeded (429).
            OpenBBFxProviderNotFoundError:      Currency pair not found (404).
            OpenBBFxProviderTimeoutError:       Request timed out.
            OpenBBFxProviderInvalidResponseError: Malformed response.
            OpenBBFxProviderError:              Other provider errors.
        """
        if self._degraded:
            msg = (
                "OpenBB API key not configured -- provider is in degraded mode"
            )
            raise OpenBBFxProviderAuthError(msg)

        base = base.strip().upper()
        quote = quote.strip().upper()

        await self._throttle()

        try:
            response = await self.http_client.get(
                "/api/v1/market/forex",
                params={"base": base, "quote": quote},
            )
        except httpx.TimeoutException as exc:
            msg = f"Request timed out after {self._timeout}s for {base}/{quote}"
            raise OpenBBFxProviderTimeoutError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"HTTP request failed for {base}/{quote}: {exc}"
            raise OpenBBFxProviderError(msg) from exc

        # -- Handle HTTP status codes ------------------------------------------
        if response.status_code == 401:
            msg = f"Invalid or expired OpenBB API key (401) for {base}/{quote}"
            raise OpenBBFxProviderAuthError(msg)
        if response.status_code == 403:
            msg = (
                f"Access denied -- API key lacks permission for FX rates (403)"
                f" on {base}/{quote}"
            )
            raise OpenBBFxProviderAuthError(msg)
        if response.status_code == 404:
            msg = f"Currency pair {base}/{quote} not found (404)"
            raise OpenBBFxProviderNotFoundError(msg)
        if response.status_code == 429:
            msg = f"OpenBB API rate limit exceeded for {base}/{quote}"
            raise OpenBBFxProviderRateLimitError(msg)

        # Any other non-2xx
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            msg = (
                f"OpenBB API returned HTTP {response.status_code}"
                f" for {base}/{quote}"
            )
            raise OpenBBFxProviderError(msg) from exc

        # -- Parse response body -----------------------------------------------
        try:
            data: dict[str, Any] = response.json()
        except (ValueError, TypeError, Exception) as exc:
            msg = f"Invalid JSON response for {base}/{quote}: {exc}"
            raise OpenBBFxProviderInvalidResponseError(msg) from exc

        rate_raw = data.get("rate")
        if rate_raw is None:
            msg = (
                f"Response missing 'rate' field for {base}/{quote}: "
                f"keys={list(data.keys())}"
            )
            raise OpenBBFxProviderInvalidResponseError(msg)

        try:
            rate = Decimal(str(rate_raw))
        except (ValueError, TypeError, ArithmeticError) as exc:
            msg = f"Invalid rate value {rate_raw!r} for {base}/{quote}: {exc}"
            raise OpenBBFxProviderInvalidResponseError(msg) from exc

        logger.debug(
            "openbb_fx_rate_fetched",
            base_currency=base,
            quote_currency=quote,
            rate=rate,
        )
        return rate

    async def close(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    # -- Internal helpers ------------------------------------------------------

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-initialised HTTP client for the OpenBB API.

        The client is created on first access with the configured
        base URL, timeout, and auth headers.
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                headers=self._build_headers(),
            )
        return self._http_client

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for OpenBB API requests."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "finance-sync/0.5.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _throttle(self) -> None:
        """Apply sliding-window rate limiting.

        Blocks the coroutine until the request rate drops below
        ``max_requests_per_second`` within a 1-second sliding window.
        """
        if self._rate_limit_max <= 0:
            return

        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            cutoff = now - self._rate_limit_window

            # Prune timestamps outside the sliding window
            self._request_timestamps = [
                t for t in self._request_timestamps if t > cutoff
            ]

            if len(self._request_timestamps) >= self._rate_limit_max:
                # Sleep until the oldest timestamp falls out of the window
                oldest = self._request_timestamps[0]
                sleep_for = oldest + self._rate_limit_window - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                # Re-prune after sleeping
                now = asyncio.get_event_loop().time()
                self._request_timestamps = [
                    t
                    for t in self._request_timestamps
                    if t > now - self._rate_limit_window
                ]

            self._request_timestamps.append(now)
