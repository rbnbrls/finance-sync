"""Currency conversion utility leveraging FxService for portfolio conversion.

Provides batch and single-currency conversion functions with efficient
rate deduplication and clear error handling for missing exchange rates,
including indirect-path resolution (e.g. EUR → GBP → USD) when a
direct exchange rate is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from finance_sync.enrichment.models import FxConversionRequest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from finance_sync.services.fx_service import FxService


logger = structlog.get_logger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────


class CurrencyConversionError(Exception):
    """Raised when a currency conversion cannot be performed."""


class NoRateError(CurrencyConversionError):
    """Raised when no exchange rate is available for a required pair."""


# ── Protocols / DTOs ───────────────────────────────────────────────────


@runtime_checkable
class HasCurrency(Protocol):
    """Protocol for items that carry an amount and a currency code.

    Any dataclass, ORM model, or Pydantic model with ``amount``
    (``Decimal``) and ``currency_code`` (``str``) attributes satisfies
    this protocol automatically — no inheritance needed.
    """

    amount: Decimal
    currency_code: str


@dataclass
class ConvertedItem:
    """Result of converting a single portfolio item to a target currency.

    Attributes:
        original_amount:   Amount before conversion.
        original_currency: Source currency code.
        converted_amount:  Amount after conversion (in target currency).
        target_currency:   Target currency code.
        rate_used:         Exchange rate that was applied.
    """

    original_amount: Decimal
    original_currency: str
    converted_amount: Decimal
    target_currency: str
    rate_used: Decimal = field(compare=False)


# ── Public API ─────────────────────────────────────────────────────────


async def convert_single(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    *,
    at_timestamp: datetime | None = None,
    fx_service: FxService,
) -> Decimal:
    """Convert a single amount from one currency to another.

    Args:
        amount:        The amount to convert.
        from_currency: Source ISO-4217 currency code (e.g. ``"EUR"``).
        to_currency:   Target ISO-4217 currency code (e.g. ``"USD"``).
        at_timestamp:  Optional timestamp for historical rate lookup.
        fx_service:    Initialised :class:`FxService` instance.

    Returns:
        The converted amount, rounded to 2 decimal places.

    Raises:
        NoRateError: If no exchange rate is available for the pair.
    """
    if from_currency == to_currency:
        return amount.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")

    request = FxConversionRequest(
        from_currency=from_currency,
        to_currency=to_currency,
        amount=amount,
        at_timestamp=at_timestamp,
    )

    result = await fx_service.convert(request)
    if result is None:
        logger.warning(
            "conversion_rate_unavailable",
            from_currency=from_currency,
            to_currency=to_currency,
        )
        msg = (
            f"No exchange rate available for {from_currency}"
            f" \u2192 {to_currency}"
        )
        raise NoRateError(msg)

    return result.converted_amount


# ── Intermediate currencies for indirect path resolution ────────────────
#
# Currencies tried as intermediaries when a direct FX rate is unavailable.
# Ordered by liquidity (most traded first) so the first successful
# cross-rate is also the most reliable.
_INDIRECT_PATH_INTERMEDIARIES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "CHF",
    "JPY",
    "CAD",
    "AUD",
    "NZD",
)


async def convert_currency_rate(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    *,
    at_timestamp: datetime | None = None,
    fx_service: FxService,
) -> Decimal:
    """Convert an amount using FxService.get_rate with indirect-path fallback.

    Tries the direct exchange rate first via ``FxService.get_rate()``.
    If the direct rate is unavailable, attempts to find an indirect path
    through a common intermediate currency (e.g. EUR → USD → GBP when
    the direct EUR → GBP rate is missing).

    Args:
        amount:        The monetary amount to convert.
        from_currency: Source ISO-4217 currency code (e.g. ``"EUR"``).
        to_currency:   Target ISO-4217 currency code (e.g. ``"USD"``).
        at_timestamp:  Optional timestamp for historical rate lookup.
        fx_service:    Initialised :class:`FxService` instance.

    Returns:
        The converted amount, rounded to 2 decimal places.

    Raises:
        NoRateError: If no exchange rate is available through any
            resolution path (direct or indirect).
    """
    if from_currency == to_currency:
        return amount.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")

    from_code = from_currency.upper()
    to_code = to_currency.upper()

    # ── 1. Direct path ────────────────────────────────────────────────
    direct = await fx_service.get_rate(
        from_code,
        to_code,
        at_timestamp=at_timestamp,
    )
    if direct is not None:
        logger.debug(
            "conversion_direct_rate",
            from_currency=from_code,
            to_currency=to_code,
            rate=direct.rate,
        )
        return (amount * direct.rate).quantize(
            Decimal("0.01"),
            rounding="ROUND_HALF_UP",
        )

    # ── 2. Indirect path through an intermediate currency ──────────────
    for intermediary in _INDIRECT_PATH_INTERMEDIARIES:
        if intermediary in (from_code, to_code):
            continue

        leg1 = await fx_service.get_rate(
            from_code,
            intermediary,
            at_timestamp=at_timestamp,
        )
        if leg1 is None:
            continue

        leg2 = await fx_service.get_rate(
            intermediary,
            to_code,
            at_timestamp=at_timestamp,
        )
        if leg2 is None:
            continue

        cross_rate = leg1.rate * leg2.rate
        logger.info(
            "conversion_indirect_path",
            from_currency=from_code,
            to_currency=to_code,
            intermediary=intermediary,
            leg1_rate=leg1.rate,
            leg2_rate=leg2.rate,
            cross_rate=cross_rate,
        )
        return (amount * cross_rate).quantize(
            Decimal("0.01"),
            rounding="ROUND_HALF_UP",
        )

    # ── 3. All paths exhausted ────────────────────────────────────────
    logger.warning(
        "conversion_rate_unavailable",
        from_currency=from_code,
        to_currency=to_code,
    )
    msg = (
        f"No exchange rate available for {from_code} → {to_code}. "
        f"Direct rate and indirect paths through "
        f"{', '.join(_INDIRECT_PATH_INTERMEDIARIES)} were exhausted."
    )
    raise NoRateError(msg)


async def convert_portfolio_items(
    items: Sequence[HasCurrency],
    target_currency: str,
    *,
    at_timestamp: datetime | None = None,
    fx_service: FxService,
) -> list[ConvertedItem]:
    """Convert a batch of portfolio items to a target currency.

    Optimises API calls by deduplicating source currencies — rates are
    fetched once per unique ``currency_code`` rather than per item.

    Args:
        items:           Sequence of objects exposing ``amount`` (Decimal)
                         and ``currency_code`` (str).
        target_currency: Target ISO-4217 currency to convert into.
        at_timestamp:    Optional timestamp for historical rate lookup.
        fx_service:      Initialised :class:`FxService` instance.

    Returns:
        A list of :class:`ConvertedItem` — one per input item, in the
        same order as ``items``.

    Raises:
        NoRateError: If any required exchange rate is unavailable.
    """
    # ── Step 1: gather unique source currencies ──────────────────────
    unique_currencies: set[str] = {
        item.currency_code for item in items
    }

    # Identity-map: currency → rate (Decimal).  Iterate in sorted order
    # for deterministic behaviour (callers and tests expect it).
    rates: dict[str, Decimal] = {}
    for currency in sorted(unique_currencies):
        if currency == target_currency:
            rates[currency] = Decimal(1)
            continue
        request = FxConversionRequest(
            from_currency=currency,
            to_currency=target_currency,
            amount=Decimal(1),
            at_timestamp=at_timestamp,
        )
        result = await fx_service.convert(request)
        if result is None:
            msg = (
                f"No exchange rate available for "
                f"{currency} \u2192 {target_currency}"
            )
            raise NoRateError(msg)
        rates[currency] = result.rate_used

    # ── Step 2: apply rates to every item ────────────────────────────
    converted: list[ConvertedItem] = []
    for item in items:
        rate = rates[item.currency_code]
        converted_amount = (item.amount * rate).quantize(
            Decimal("0.01"),
            rounding="ROUND_HALF_UP",
        )
        converted.append(
            ConvertedItem(
                original_amount=item.amount,
                original_currency=item.currency_code,
                converted_amount=converted_amount,
                target_currency=target_currency,
                rate_used=rate,
            )
        )

    return converted
