"""FX (foreign exchange) service for multi-currency portfolio support.

Provides:
- Fetching and caching exchange rates from OpenBB / fallback sources
- Currency conversion for arbitrary amounts
- Historical rate lookups for time-weighted conversions
- Graceful degradation when the data source is unavailable
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.enrichment.models import (
    FxConversionRequest,
    FxConversionResult,
    FxRateObservation,
)
from finance_sync.models.fx_rate import FxRate

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings
    from finance_sync.db.uow import UnitOfWork

# Import the OpenBB provider at runtime to avoid circular imports
try:
    from finance_sync.providers.openbb_fx import (
        OpenBBFxProvider,
        OpenBBFxProviderNotFoundError,
        OpenBBFxProviderRateLimitError,
        OpenBBFxProviderTimeoutError,
    )

    _HAS_OPENBB_PROVIDER = True
except ImportError:
    _HAS_OPENBB_PROVIDER = False
    OpenBBFxProvider = None  # type: ignore[assignment,misc]


logger = structlog.get_logger(__name__)


class FxServiceError(Exception):
    """Base exception for FX service errors."""


class FxRateNotFoundError(FxServiceError):
    """Raised when no exchange rate is available for a currency pair."""


class FxRateFetchError(FxServiceError):
    """Raised when fetching an exchange rate from the upstream API fails."""


class InvalidCurrencyError(FxServiceError, ValueError):
    """Raised when an invalid or unsupported currency code is provided."""


@dataclass
class _CacheEntry:
    """An entry in the in-memory FX rate cache.

    Attributes:
        observation: The cached rate observation.
        expires_at:  Unix timestamp (``time.monotonic()``) when this entry
                     is considered stale.
    """

    observation: FxRateObservation
    expires_at: float


logger = structlog.get_logger(__name__)


class FxServiceError(Exception):
    """Base exception for FX service errors."""


class FxRateNotFoundError(FxServiceError):
    """Raised when no exchange rate is available for a currency pair."""


class FxRateFetchError(FxServiceError):
    """Raised when fetching an exchange rate from the upstream API fails."""


class InvalidCurrencyError(FxServiceError, ValueError):
    """Raised when an invalid or unsupported currency code is provided."""


@dataclass
class _CacheEntry:
    """An entry in the in-memory FX rate cache.

    Attributes:
        observation: The cached rate observation.
        expires_at:  Unix timestamp (``time.monotonic()``) when this entry
                     is considered stale.
    """

    observation: FxRateObservation
    expires_at: float


class FxService:
    """Service for foreign exchange rate management and currency conversion.

    Fetches rates from OpenBB (when configured), falls back to local
    database cache, and supports full historical lookups.
    """

    # Common currency pairs that are often quoted inversely on the wire.
    # The service normalises these to ensure consistent (base -> quote) storage.
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

    # In-memory cache TTL as a fraction of the DB cache TTL -- the memory
    # cache acts as an L1 hot cache that expires much sooner than the DB
    # (L2) so that stale rates aren't served for long.
    MEMORY_CACHE_TTL_MULTIPLIER: float = 0.1  # 10% of DB cache TTL

    # Hardcoded fallback rates used when neither the in-memory cache, the
    # database, nor the upstream API can provide a rate.  These are rough
    # market-approximate values so the system never silently drops a
    # conversion; the trade-off is approximate accuracy.
    FALLBACK_RATES: dict[tuple[str, str], Decimal] = {
        ("EUR", "USD"): Decimal("1.09"),
        ("USD", "EUR"): Decimal("0.9174"),
        ("GBP", "USD"): Decimal("1.27"),
        ("USD", "GBP"): Decimal("0.7874"),
        ("EUR", "GBP"): Decimal("0.86"),
        ("GBP", "EUR"): Decimal("1.1628"),
        ("USD", "JPY"): Decimal("149.50"),
        ("EUR", "JPY"): Decimal("162.96"),
        ("USD", "CHF"): Decimal("0.88"),
        ("EUR", "CHF"): Decimal("0.96"),
        ("USD", "CAD"): Decimal("1.36"),
        ("EUR", "CAD"): Decimal("1.48"),
        ("AUD", "USD"): Decimal("0.66"),
        ("NZD", "USD"): Decimal("0.61"),
    }

    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        *,
        provider: Any | None = None,
    ) -> None:
        self._settings = settings
        self._uow = uow

        self._degraded = settings.openbb_api_key is None

        # In-memory cache: { (base_currency, quote_currency): _CacheEntry }
        self._memory_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._cache_lock: asyncio.Lock = asyncio.Lock()

        # Use provided provider or create one from settings
        self._provider = provider
        if (
            not self._degraded
            and self._provider is None
            and _HAS_OPENBB_PROVIDER
        ):
            api_key = (
                settings.openbb_api_key.get_secret_value()
                if settings.openbb_api_key
                else None
            )
            self._provider = OpenBBFxProvider(
                api_key=api_key,
                base_url=settings.openbb_base_url,
                max_requests_per_second=settings.openbb_rate_limit_rps,
                request_timeout=settings.openbb_request_timeout,
            )

        if self._degraded:
            logger.warning(
                "fx_service_degraded",
                reason="no_openbb_api_key",
                message=(
                    "OpenBB API key not configured -- "
                    "FX rates limited to cached data."
                ),
            )
        else:
            logger.info(
                "fx_service_initialised",
                ttl_seconds=settings.fx_rate_cache_ttl_seconds,
                provider_type=type(self._provider).__name__
                if self._provider
                else "built-in",
            )

    # -- Public API ---------------------------------------------------------

    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        at_timestamp: datetime | None = None,
    ) -> FxRateObservation | None:
        """Get an exchange rate for a currency pair.

        Resolution order (``at_timestamp`` is None / non-historical):
          1. In-memory cache (L1, fastest, 10 % of DB TTL)
          2. Local database cache (L2)
          3. OpenBB API (when configured)
          4. Hardcoded fallback rate

        When ``at_timestamp`` is provided the in-memory cache and fallback
        rates are bypassed -- the lookup uses only the database and API.

        Args:
            base_currency: ISO-4217 base currency code.
            quote_currency: ISO-4217 quote currency code.
            at_timestamp: Optional timestamp for historical lookup.
                When omitted, returns the most recent rate.

        Returns:
            An FxRateObservation, or None if the rate is unavailable.
        """
        if base_currency == quote_currency:
            logger.debug(
                "fx_rate_identity",
                base_currency=base_currency,
            )
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

        # -- Layer 1: In-memory cache (non-historical only) -----------------
        if at_timestamp is None:
            cached = await self._check_memory_cache(base, quote)
            if cached is not None:
                logger.debug(
                    "fx_rate_memory_hit",
                    base_currency=base,
                    quote_currency=quote,
                    rate=cached.rate,
                )
                return cached.inverse() if inverted else cached

        # -- Layer 2: Local database ----------------------------------------
        local = await self._lookup_local_rate(
            base, quote, at_timestamp=at_timestamp
        )
        if local is not None:
            logger.debug(
                "fx_rate_local_hit",
                base_currency=base,
                quote_currency=quote,
                rate=local.rate,
            )
            # Prime the in-memory cache on DB hit
            if at_timestamp is None:
                await self._set_memory_cache(local)
            return local.inverse() if inverted else local

        # Also try the inverse pair if the canonical direction didn't match
        if (base, quote) != (quote, base):
            inv_base, inv_quote = quote, base
            local_inv = await self._lookup_local_rate(
                inv_base, inv_quote, at_timestamp=at_timestamp
            )
            if local_inv is not None:
                result = local_inv.inverse()
                if at_timestamp is None:
                    await self._set_memory_cache(result)
                return result if not inverted else result.inverse()

        # -- Layer 3: OpenBB API --------------------------------------------
        if not self._degraded:
            logger.debug(
                "fx_rate_api_fetch",
                base_currency=base,
                quote_currency=quote,
            )
            api_rate = await self._fetch_rate_from_api(base, quote)
            if api_rate is not None:
                await self._store_rate(api_rate)
                if at_timestamp is None:
                    await self._set_memory_cache(api_rate)
                logger.info(
                    "fx_rate_api_success",
                    base_currency=base,
                    quote_currency=quote,
                    rate=api_rate.rate,
                )
                return api_rate.inverse() if inverted else api_rate
            logger.warning(
                "fx_rate_api_failed",
                base_currency=base,
                quote_currency=quote,
            )

        # -- Layer 4: Hardcoded fallback (non-historical only) --------------
        if at_timestamp is None:
            fallback = self._get_fallback_rate(base, quote)
            if fallback is not None:
                logger.info(
                    "fx_rate_fallback",
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    rate=fallback.rate,
                )
                return fallback.inverse() if inverted else fallback

        logger.info(
            "fx_rate_unavailable",
            base_currency=base_currency,
            quote_currency=quote_currency,
        )
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
            logger.debug(
                "fx_convert_identity",
                from_currency=request.from_currency,
            )
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
            A dict mapping target currency -> rate, or empty dict on failure.
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

    async def fetch_latest_rates(
        self,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], FxRateObservation]:
        """Fetch latest rates for a list of explicit currency pairs.

        Each pair is ``(base_currency, quote_currency)``.  Pairs are
        resolved through the standard layered cache (memory -> DB ->
        API -> fallback).  Results are returned even when some pairs
        fail -- only successfully resolved pairs are included.

        Args:
            pairs:  List of ``(base_currency, quote_currency)`` tuples
                    to fetch rates for.

        Returns:
            A dict mapping each successfully resolved pair to its
            :class:`FxRateObservation`.  Pairs that could not be
            resolved are omitted.
        """
        results: dict[tuple[str, str], FxRateObservation] = {}
        for base, quote in pairs:
            obs = await self.get_rate(base.upper(), quote.upper())
            if obs is not None:
                results[(base.upper(), quote.upper())] = obs
        return results

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

    async def fetch_and_cache_rates(
        self,
        base_currencies: list[str] | None = None,
    ) -> int:
        """Bulk-fetch and cache daily rates for common base currencies.

        This is designed to be called periodically by a scheduled job
        (e.g. once per day) to pre-populate the FX cache with rates for
        all major pairs starting from the given base currencies.

        Args:
            base_currencies: List of ISO-4217 base currency codes to
                fetch rates for.  Defaults to ``["EUR", "USD", "GBP"]``
                which covers the majority of portfolio conversions.

        Returns:
            Number of rate observations successfully fetched and cached.
        """
        if base_currencies is None:
            base_currencies = ["EUR", "USD", "GBP"]

        count = 0
        for base in base_currencies:
            results = await self.fetch_all_major_rates(base_currency=base)
            count += len(results)

        logger.info(
            "fx_rates_bulk_fetched",
            base_currencies=base_currencies,
            total_rates=count,
        )
        return count

    # -- Local storage ------------------------------------------------------

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

        When looking up the most recent rate (no specific timestamp),
        the cache TTL is enforced: stale entries older than
        ``fx_rate_cache_ttl_seconds`` are ignored so the caller
        can re-fetch from the upstream API.
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

        # Enforce cache TTL for the "latest rate" case
        if at_timestamp is None:
            age = datetime.now(UTC) - row.timestamp
            ttl = timedelta(seconds=self._settings.fx_rate_cache_ttl_seconds)
            if age > ttl:
                logger.info(
                    "fx_cache_stale",
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    age_seconds=age.total_seconds(),
                    ttl_seconds=ttl.total_seconds(),
                )
                return None

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

    # -- In-memory cache (L1) ----------------------------------------------

    async def _check_memory_cache(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> FxRateObservation | None:
        """Check the in-memory cache for a non-stale rate.

        Thread-safe via ``asyncio.Lock``.
        """
        key = (base_currency, quote_currency)
        async with self._cache_lock:
            entry = self._memory_cache.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._memory_cache[key]
                return None
            return entry.observation

    async def _set_memory_cache(
        self,
        observation: FxRateObservation,
    ) -> None:
        """Store a rate in the in-memory cache with TTL.

        Thread-safe via ``asyncio.Lock``.

        The memory TTL is computed as a fraction
        (``MEMORY_CACHE_TTL_MULTIPLIER``) of the DB cache TTL so that
        the hot cache turns over faster than the persistent one.
        """
        key = (observation.base_currency, observation.quote_currency)
        ttl = (
            self._settings.fx_rate_cache_ttl_seconds
            * self.MEMORY_CACHE_TTL_MULTIPLIER
        )
        expires_at = time.monotonic() + ttl
        async with self._cache_lock:
            self._memory_cache[key] = _CacheEntry(
                observation=observation,
                expires_at=expires_at,
            )

    # -- Fallback rates ----------------------------------------------------

    def _get_fallback_rate(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> FxRateObservation | None:
        """Return a hardcoded fallback rate for a currency pair.

        Used as a last resort when the in-memory cache, database, and
        upstream API all fail.  Only covers major currency pairs.
        """
        key = (base_currency.upper(), quote_currency.upper())
        rate = self.FALLBACK_RATES.get(key)
        if rate is not None:
            return FxRateObservation(
                base_currency=base_currency.upper(),
                quote_currency=quote_currency.upper(),
                rate=rate,
                timestamp=datetime.now(UTC),
                source="fallback",
            )
        return None

    def _row_to_observation(self, row: FxRate) -> FxRateObservation:
        """Convert an FxRate ORM row to an FxRateObservation DTO."""
        return FxRateObservation(
            base_currency=row.base_currency,
            quote_currency=row.quote_currency,
            rate=row.rate,
            timestamp=row.timestamp,
            source=row.source,
        )

    # -- API fetch ---------------------------------------------------------

    async def _fetch_rate_from_api(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> FxRateObservation | None:
        """Fetch an exchange rate via the OpenBB provider.

        Delegates to the configured provider (OpenBBFxProvider) and wraps
        the result in an FxRateObservation.

        Args:
            base_currency: Normalised base currency code.
            quote_currency: Normalised quote currency code.

        Returns:
            An FxRateObservation, or None on failure.
        """
        if self._provider is None:
            logger.error(
                "fx_api_no_provider",
                pair=f"{base_currency}/{quote_currency}",
            )
            return None

        try:
            rate = await self._provider.get_latest_rate(
                base_currency, quote_currency,
            )

            return FxRateObservation(
                base_currency=base_currency,
                quote_currency=quote_currency,
                rate=rate,
                timestamp=datetime.now(UTC),
                source="openbb",
            )
        except OpenBBFxProviderNotFoundError:
            logger.debug(
                "fx_api_404",
                pair=f"{base_currency}/{quote_currency}",
            )
            return None
        except OpenBBFxProviderTimeoutError:
            logger.warning(
                "fx_api_timeout",
                pair=f"{base_currency}/{quote_currency}",
                timeout=self._settings.openbb_request_timeout,
            )
            return None
        except OpenBBFxProviderRateLimitError:
            logger.warning(
                "fx_api_rate_limited",
                pair=f"{base_currency}/{quote_currency}",
            )
            return None
        except Exception as exc:
            logger.warning(
                "fx_api_error",
                pair=f"{base_currency}/{quote_currency}",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    # -- Pair normalisation -------------------------------------------------

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
        # Same currency -- identity
        if base == quote:
            return base, quote, False

        # Check if the pair is a known major in the requested direction
        if (base, quote) in FxService.MAJOR_PAIRS:
            return base, quote, False

        # Check if reversing makes it a known major
        if (quote, base) in FxService.MAJOR_PAIRS:
            return quote, base, True  # inverted

        # Default: keep original order -- caller will match on storage
        return base, quote, False

    # -- Cleanup -----------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying provider (idempotent)."""
        if self._provider is not None:
            await self._provider.close()


# -- Helper ------------------------------------------------------------------


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


async def convert_currency(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    fx_service: FxService,
) -> Decimal:
    """Convert an amount from one currency to another.

    A lightweight convenience wrapper around ``FxService.convert()``
    that accepts primitive arguments and raises on missing rates instead
    of returning ``None``.  Intended as the currency-conversion primitive
    consumed by Phase 4 allocation and performance modules.

    Args:
        amount:         The monetary amount to convert.
        from_currency:  ISO-4217 source currency code (e.g. ``"EUR"``).
        to_currency:    ISO-4217 target currency code (e.g. ``"USD"``).
        fx_service:     An initialised :class:`FxService` instance.

    Returns:
        The converted amount, rounded to 2 decimal places.

    Raises:
        FxRateNotFoundError: If no exchange rate is available for the
            requested currency pair (all resolution layers exhausted).

    Example:
        >>> from decimal import Decimal
        >>> from finance_sync.services.fx_service import convert_currency
        >>> # Assuming ``service`` is an initialised FxService instance:
        >>> result = await convert_currency(
        ...     Decimal("100.00"), "EUR", "USD", service,
        ... )
        >>> isinstance(result, Decimal)
        True
    """
    if from_currency == to_currency:
        return amount

    request = FxConversionRequest(
        from_currency=from_currency,
        to_currency=to_currency,
        amount=amount,
    )

    result = await fx_service.convert(request)

    if result is None:
        msg = (
            f"No exchange rate available for {from_currency} -> "
            f"{to_currency}. All resolution layers (memory cache, "
            "local DB, upstream API, fallback rates) were exhausted."
        )
        raise FxRateNotFoundError(msg)

    return result.converted_amount
