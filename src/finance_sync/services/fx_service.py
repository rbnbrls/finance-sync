"""FX (foreign exchange) service for multi-currency portfolio support.

Provides:
- Fetching and caching exchange rates from OpenBB / fallback sources
- Currency conversion for arbitrary amounts
- Historical rate lookups for time-weighted conversions
- Graceful degradation when the data source is unavailable
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from finance_sync.enrichment.models import (
    FxConversionRequest,
    FxConversionResult,
    FxRateObservation,
)
from finance_sync.models.fx_rate import FxRate

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings
    from finance_sync.db.uow import UnitOfWork


class FxService:
    """Service for foreign exchange rate management and currency conversion.

    Fetches rates from OpenBB (when configured), falls back to local
    database cache, and supports full historical lookups.
    """

    # Common currency pairs that are often quoted inversely on the wire.
    # The service normalises these to ensure consistent (base → quote) storage.
    MAJOR_PAIRS: set[tuple[str, str]] = {
        ("EUR", "USD"),
        ("USD", "JPY"),
        ("GBP", "USD"),
        ("USD", "CHF"),
        ("USD", "CAD"),
        ("AUD", "USD"),
        ("NZD", "USD"),
        ("EUR", "GBP"),
        ("EUR", "JPY"),
    }

    # Currencies where the market convention is to quote as X per USD
    # (inverse of the normal pair direction).
    INDIRECT_QUOTE_CURRENCIES: set[str] = {"JPY", "CHF", "CAD"}

    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
    ) -> None:
        self._settings = settings
        self._uow = uow

        self._http_client: httpx.AsyncClient | None = None
        self._degraded = settings.openbb_api_key is None

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client for FX rate API calls."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self._settings.openbb_base_url,
                timeout=httpx.Timeout(self._settings.openbb_request_timeout),
                headers=self._build_headers(),
            )
        return self._http_client

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for FX API requests."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "finance-sync/0.1.0",
        }
        if self._settings.openbb_api_key:
            api_key = self._settings.openbb_api_key.get_secret_value()
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    # ── Public API ────────────────────────────────────────────────────

    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        at_timestamp: datetime | None = None,
    ) -> FxRateObservation | None:
        """Get an exchange rate for a currency pair.

        Args:
            base_currency: ISO-4217 base currency code.
            quote_currency: ISO-4217 quote currency code.
            at_timestamp: Optional timestamp for historical lookup.
                When omitted, returns the most recent rate.

        Returns:
            An FxRateObservation, or None if the rate is unavailable.
        """
        if base_currency == quote_currency:
            return FxRateObservation(
                base_currency=base_currency,
                quote_currency=quote_currency,
                rate=Decimal(1),
                timestamp=at_timestamp or datetime.now(UTC),
                source="identity",
            )

        # Canonicalise the pair direction
        base, quote, inverted = self._canonicalise_pair(
            base_currency, quote_currency
        )

        # Try local DB first with canonicalised pair
        local = await self._lookup_local_rate(
            base, quote, at_timestamp=at_timestamp
        )
        if local is not None:
            return local.inverse() if inverted else local

        # Also try the inverse pair if the canonical direction didn't match
        if (base, quote) != (quote, base):
            inv_base, inv_quote = quote, base
            local_inv = await self._lookup_local_rate(
                inv_base, inv_quote, at_timestamp=at_timestamp
            )
            if local_inv is not None:
                # The local has it in the opposite direction, invert back
                result = local_inv.inverse()
                return result if not inverted else result.inverse()

        # Fall back to API
        if not self._degraded:
            api_rate = await self._fetch_rate_from_api(base, quote)
            if api_rate is not None:
                # Store for future lookups
                await self._store_rate(api_rate)
                return api_rate.inverse() if inverted else api_rate

        return None

    async def convert(
        self, request: FxConversionRequest
    ) -> FxConversionResult | None:
        """Convert an amount from one currency to another.

        Args:
            request: The conversion request specifying from/to currencies,
                amount, and optional timestamp.

        Returns:
            An FxConversionResult with the converted amount, or None
            if no rate is available for the requested pair.
        """
        if request.from_currency == request.to_currency:
            return FxConversionResult(
                from_currency=request.from_currency,
                to_currency=request.to_currency,
                original_amount=request.amount,
                converted_amount=request.amount,
                rate_used=Decimal(1),
                rate_timestamp=request.at_timestamp or datetime.now(UTC),
                source="identity",
            )

        rate_obs = await self.get_rate(
            request.from_currency,
            request.to_currency,
            at_timestamp=request.at_timestamp,
        )

        if rate_obs is None:
            return None

        converted = (request.amount * rate_obs.rate).quantize(
            Decimal("0.01"),
            rounding="ROUND_HALF_UP",
        )

        return FxConversionResult(
            from_currency=request.from_currency,
            to_currency=request.to_currency,
            original_amount=request.amount,
            converted_amount=converted,
            rate_used=rate_obs.rate,
            rate_timestamp=rate_obs.timestamp,
            source=rate_obs.source,
        )

    async def get_rates_for_base(
        self,
        base_currency: str,
        *,
        targets: list[str] | None = None,
        at_timestamp: datetime | None = None,
    ) -> dict[str, Decimal]:
        """Get exchange rates from a base currency to one or more targets.

        Args:
            base_currency: Base currency code.
            targets: List of target currency codes. When omitted, returns
                rates for all major currency pairs.
            at_timestamp: Optional timestamp for historical lookup.

        Returns:
            A dict mapping target currency → rate, or empty dict on failure.
        """
        if targets is None:
            majors = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")
            targets = [c for c in majors if c != base_currency]

        result: dict[str, Decimal] = {}
        for target in targets:
            if target == base_currency:
                result[target] = Decimal(1)
                continue
            rate_obs = await self.get_rate(
                base_currency, target, at_timestamp=at_timestamp
            )
            if rate_obs is not None:
                result[target] = rate_obs.rate

        return result

    async def fetch_all_major_rates(
        self,
        *,
        base_currency: str = "EUR",
    ) -> list[FxRateObservation]:
        """Fetch and store rates for all major currency pairs.

        This is designed to be called periodically by a scheduled job
        to pre-populate the FX rate cache.

        Args:
            base_currency: Base currency for rate fetches. Defaults to EUR.

        Returns:
            List of fetched rate observations.
        """
        targets = ["USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]
        observations: list[FxRateObservation] = []

        for target in targets:
            if target == base_currency:
                continue
            obs = await self.get_rate(base_currency, target)
            if obs is not None:
                observations.append(obs)

        return observations

    # ── Local storage ─────────────────────────────────────────────────

    async def _lookup_local_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        at_timestamp: datetime | None = None,
    ) -> FxRateObservation | None:
        """Look up a rate from the local database.

        Returns the closest rate at or before `at_timestamp`, or the
        most recent rate when `at_timestamp` is None.
        """
        filters = [
            FxRate.base_currency == base_currency,  # type: ignore[attr-defined]
            FxRate.quote_currency == quote_currency,  # type: ignore[attr-defined]
        ]
        order = FxRate.timestamp.desc()  # type: ignore[attr-defined]
        limit = 1

        if at_timestamp is not None:
            filters.append(
                FxRate.timestamp <= at_timestamp  # type: ignore[attr-defined]
            )

        rows = await self._uow.fx_rates.list(
            *filters,
            order_by=order,
            limit=limit,
        )
        if not rows:
            return None

        row = rows[0]
        return self._row_to_observation(row)

    async def _store_rate(self, observation: FxRateObservation) -> None:
        """Persist an FX rate observation, deduplicating.

        Deduplication is handled by the unique constraint on
        (base_currency, quote_currency, timestamp, source).
        """
        existing = await self._uow.fx_rates.list(
            FxRate.base_currency == observation.base_currency,  # type: ignore[attr-defined]
            FxRate.quote_currency == observation.quote_currency,  # type: ignore[attr-defined]
            FxRate.timestamp == observation.timestamp,  # type: ignore[attr-defined]
            FxRate.source == observation.source,  # type: ignore[attr-defined]
            limit=1,
        )
        if existing:
            return  # already stored

        fx_rate = FxRate(
            base_currency=observation.base_currency,
            quote_currency=observation.quote_currency,
            rate=observation.rate,
            timestamp=observation.timestamp,
            source=observation.source,
        )
        await self._uow.fx_rates.add(fx_rate)

    def _row_to_observation(self, row: FxRate) -> FxRateObservation:
        """Convert an FxRate ORM row to an FxRateObservation DTO."""
        return FxRateObservation(
            base_currency=row.base_currency,
            quote_currency=row.quote_currency,
            rate=row.rate,
            timestamp=row.timestamp,
            source=row.source,
        )

    # ── API fetch ─────────────────────────────────────────────────────

    async def _fetch_rate_from_api(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> FxRateObservation | None:
        """Fetch an exchange rate from the OpenBB API.

        Args:
            base_currency: Normalised base currency code.
            quote_currency: Normalised quote currency code.

        Returns:
            An FxRateObservation, or None on failure.
        """
        try:
            response = await self.http_client.get(
                f"/api/{self._settings.openbb_api_version}/market/forex",
                params={
                    "base": base_currency,
                    "quote": quote_currency,
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            return FxRateObservation(
                base_currency=data.get("base", base_currency),
                quote_currency=data.get("quote", quote_currency),
                rate=_safe_decimal(data.get("rate")),
                timestamp=_parse_timestamp(data.get("timestamp")),
                source=data.get("source", "openbb"),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            return None
        except httpx.TimeoutException:
            return None
        except httpx.HTTPError:
            return None

    # ── Pair normalisation ────────────────────────────────────────────

    @staticmethod
    def _canonicalise_pair(
        base: str,
        quote: str,
    ) -> tuple[str, str, bool]:
        """Normalise a currency pair to a standard direction.

        Returns (base, quote, inverted) where:
        - base, quote are the canonicalised pair order
        - inverted is True if the original pair was reversed

        The canonical order tries major-pair convention: if one of the
        currencies is USD, it prefers quoting as EUR/USD or GBP/USD
        rather than USD/EUR.
        """
        # Same currency — identity
        if base == quote:
            return base, quote, False

        # Check if the pair is a known major in the requested direction
        if (base, quote) in FxService.MAJOR_PAIRS:
            return base, quote, False

        # Check if reversing makes it a known major
        if (quote, base) in FxService.MAJOR_PAIRS:
            return quote, base, True  # inverted

        # Default: keep original order — caller will match on storage
        return base, quote, False

    # ── Cleanup ───────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()


# ── Helpers ─────────────────────────────────────────────────────────────


def _safe_decimal(value: Any) -> Decimal | None:
    """Safely convert a value to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _parse_timestamp(raw: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime."""
    if not raw:
        return datetime.now(UTC)
    try:
        cleaned = raw.rstrip("Z")
        if not cleaned:
            return datetime.now(UTC)
        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return datetime.now(UTC)
