"""Tests for the currency converter utility — batch and
single-currency conversion.

Uses a mocked FxService so no API or database is needed.
"""
# pyright: basic

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import (
    FxConversionRequest,
    FxConversionResult,
    FxRateObservation,
)
from finance_sync.utils.currency_converter import (
    ConvertedItem,
    CurrencyConversionError,
    HasCurrency,
    NoRateError,
    convert_currency_rate,
    convert_portfolio_items,
    convert_single,
)

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_fx_service() -> MagicMock:
    """Return a pre-configured mock FxService that rejects all conversions."""
    svc = MagicMock()
    svc.convert = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def recent_ts() -> datetime:
    """A stable 'now' timestamp for rate observations."""
    return datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _make_result(
    from_currency: str,
    to_currency: str,
    amount: Decimal,
    rate: Decimal,
    ts: datetime,
) -> FxConversionResult:
    """Build an FxConversionResult for the mock service to return."""
    converted = (amount * rate).quantize(
        Decimal("0.01"), rounding="ROUND_HALF_UP"
    )
    return FxConversionResult(
        from_currency=from_currency,
        to_currency=to_currency,
        original_amount=amount,
        converted_amount=converted,
        rate_used=rate,
        rate_timestamp=ts,
        source="test_mock",
    )


def _rate_dispatch(
    rates: dict[str, Decimal],
    ts: datetime,
) -> Any:
    """Return a side-effect function that returns the rate for a request."""

    async def _side_effect(
        request: FxConversionRequest,
    ) -> FxConversionResult | None:
        if request.from_currency not in rates:
            return None
        return _make_result(
            from_currency=request.from_currency,
            to_currency=request.to_currency,
            amount=request.amount,
            rate=rates[request.from_currency],
            ts=ts,
        )

    return _side_effect


# ── Test DTO for HasCurrency protocol ──────────────────────────────────


@dataclass
class _TestItem:
    """Minimal item satisfying the HasCurrency protocol."""

    amount: Decimal
    currency_code: str


class TestHasCurrencyProtocol:
    """HasCurrency is satisfied by any object with the right attributes."""

    def test_dataclass_satisfies(self) -> None:
        item = _TestItem(amount=Decimal(1000), currency_code="EUR")
        assert isinstance(item, HasCurrency)

    def test_magicmock_does_not_satisfy(self) -> None:
        """A bare MagicMock lacks the attributes and fails runtime check."""
        assert not isinstance(MagicMock(), HasCurrency)

    def test_typed_dict_satisfies_via_attrs(self) -> None:
        """Any object with .amount and .currency_code passes."""

        class _AdHoc:
            def __init__(self, amount: Decimal, code: str) -> None:
                self.amount = amount
                self.currency_code = code

        obj = _AdHoc(Decimal(500), "GBP")
        assert isinstance(obj, HasCurrency)


# ── ConvertedItem DTO ──────────────────────────────────────────────────


class TestConvertedItem:
    """ConvertedItem dataclass basics."""

    def test_create(self) -> None:
        item = ConvertedItem(
            original_amount=Decimal("100.00"),
            original_currency="EUR",
            converted_amount=Decimal("109.45"),
            target_currency="USD",
            rate_used=Decimal("1.0945"),
        )
        assert item.original_amount == Decimal("100.00")
        assert item.converted_amount == Decimal("109.45")
        assert item.original_currency == "EUR"
        assert item.target_currency == "USD"
        assert item.rate_used == Decimal("1.0945")


# ── convert_single ─────────────────────────────────────────────────────


class TestConvertSingle:
    """Tests for convert_single()."""

    async def test_same_currency(self, mock_fx_service: MagicMock) -> None:
        """Same-currency conversion returns amount unchanged."""
        result = await convert_single(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="EUR",
            fx_service=mock_fx_service,
        )
        assert result == Decimal("100.00")
        # FxService should NOT have been called
        mock_fx_service.convert.assert_not_called()

    async def test_convert_success(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Successful conversion returns converted amount."""
        mock_fx_service.convert.return_value = _make_result(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("200.00"),
            rate=Decimal("1.0945"),
            ts=recent_ts,
        )

        result = await convert_single(
            amount=Decimal("200.00"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service,
        )
        assert result == Decimal("218.90")  # 200 * 1.0945 = 218.90

    async def test_no_rate_raises(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Missing rate raises NoRateError."""
        mock_fx_service.convert.return_value = None

        with pytest.raises(NoRateError, match=r"EUR .* USD"):
            await convert_single(
                amount=Decimal("100.00"),
                from_currency="EUR",
                to_currency="USD",
                fx_service=mock_fx_service,
            )

    async def test_forwards_at_timestamp(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """at_timestamp is forwarded to FxService.convert."""
        mock_fx_service.convert.return_value = _make_result(
            from_currency="GBP",
            to_currency="EUR",
            amount=Decimal("50.00"),
            rate=Decimal("1.1700"),
            ts=recent_ts,
        )

        historical_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        await convert_single(
            amount=Decimal("50.00"),
            from_currency="GBP",
            to_currency="EUR",
            at_timestamp=historical_ts,
            fx_service=mock_fx_service,
        )

        # Verify the request carries the timestamp
        call_args = mock_fx_service.convert.call_args
        request: FxConversionRequest = call_args[0][0]
        assert request.at_timestamp == historical_ts

    async def test_rounding(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Result is rounded to 2 decimal places."""
        mock_fx_service.convert.return_value = _make_result(
            from_currency="EUR",
            to_currency="USD",
            amount=Decimal("1.23"),
            rate=Decimal("1.0945"),
            ts=recent_ts,
        )

        result = await convert_single(
            amount=Decimal("1.23"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service,
        )
        # 1.23 * 1.0945 = 1.346235 → rounded to 1.35
        assert result == Decimal("1.35")

    async def test_same_currency_quantize(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Same-currency path also returns quantized result."""
        result = await convert_single(
            amount=Decimal("100.999"),
            from_currency="USD",
            to_currency="USD",
            fx_service=mock_fx_service,
        )
        assert result == Decimal("101.00")


# ── convert_portfolio_items ────────────────────────────────────────────


class TestConvertPortfolioItems:
    """Tests for convert_portfolio_items()."""

    async def test_all_same_currency(
        self, mock_fx_service: MagicMock
    ) -> None:
        """All items already in target currency — no conversion needed."""
        items = [
            _TestItem(amount=Decimal("100.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("250.50"), currency_code="EUR"),
            _TestItem(amount=Decimal("50.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="EUR", fx_service=mock_fx_service
        )

        assert len(results) == 3
        assert all(
            r.converted_amount == r.original_amount for r in results
        )
        assert all(r.rate_used == Decimal(1) for r in results)
        # No actual conversion calls
        mock_fx_service.convert.assert_not_called()

    async def test_single_source_currency(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """All items in same source currency — one rate fetch."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.0945")}, ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("100.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("250.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )

        assert len(results) == 2
        assert results[0].converted_amount == Decimal("109.45")
        assert results[1].converted_amount == Decimal("273.63")
        assert results[0].original_currency == "EUR"
        assert results[1].target_currency == "USD"
        # convert should have been called exactly once (deduplicated)
        assert mock_fx_service.convert.call_count == 1

    async def test_multiple_source_currencies(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Items in different currencies — one fetch per unique currency."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.0945"), "GBP": Decimal("1.2700")},
            ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("100.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("200.00"), currency_code="GBP"),
            _TestItem(amount=Decimal("50.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )

        assert len(results) == 3
        assert results[0].converted_amount == Decimal("109.45")
        assert results[1].converted_amount == Decimal("254.00")
        assert results[2].converted_amount == Decimal("54.73")
        assert results[0].original_currency == "EUR"
        assert results[1].original_currency == "GBP"
        assert results[2].original_currency == "EUR"
        # Exactly 2 convert calls (EUR→USD, GBP→USD)
        assert mock_fx_service.convert.call_count == 2

    async def test_mixed_target_currency_items(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Items already in target currency are not converted."""
        items = [
            _TestItem(amount=Decimal("100.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("500.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="EUR", fx_service=mock_fx_service
        )

        assert len(results) == 2
        assert results[0].converted_amount == Decimal("100.00")
        assert results[1].converted_amount == Decimal("500.00")
        mock_fx_service.convert.assert_not_called()

    async def test_missing_rate_raises(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Missing rate for any source currency raises NoRateError."""
        mock_fx_service.convert.return_value = None

        items = [
            _TestItem(amount=Decimal("100.00"), currency_code="XYZ"),
        ]

        with pytest.raises(NoRateError, match=r"XYZ .* EUR"):
            await convert_portfolio_items(
                items,
                target_currency="EUR",
                fx_service=mock_fx_service,
            )

    async def test_forwards_at_timestamp(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """at_timestamp is forwarded for every rate fetch."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"GBP": Decimal("1.1700")}, ts=recent_ts,
        )

        historical_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        items = [_TestItem(amount=Decimal("100.00"), currency_code="GBP")]

        await convert_portfolio_items(
            items,
            target_currency="EUR",
            at_timestamp=historical_ts,
            fx_service=mock_fx_service,
        )

        call_args = mock_fx_service.convert.call_args
        request: FxConversionRequest = call_args[0][0]
        assert request.at_timestamp == historical_ts

    async def test_return_order_matches_input(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Result list maintains input order."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.09")}, ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("10.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("20.00"), currency_code="EUR"),
            _TestItem(amount=Decimal("30.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )

        assert [r.original_amount for r in results] == [
            Decimal("10.00"),
            Decimal("20.00"),
            Decimal("30.00"),
        ]
        assert [r.converted_amount for r in results] == [
            Decimal("10.90"),
            Decimal("21.80"),
            Decimal("32.70"),
        ]


class TestCurrencyConversionError:
    """Exception class hierarchy."""

    def test_no_rate_is_subtype(self) -> None:
        assert issubclass(NoRateError, CurrencyConversionError)
        assert issubclass(CurrencyConversionError, Exception)

    def test_no_rate_accepts_message(self) -> None:
        exc = NoRateError("No rate for EUR/USD")
        assert str(exc) == "No rate for EUR/USD"

    def test_currency_conversion_error_accepts_message(self) -> None:
        exc = CurrencyConversionError("Generic error")
        assert str(exc) == "Generic error"


class TestConvertPortfolioItemsEdgeCases:
    """Edge case tests for convert_portfolio_items()."""

    async def test_empty_items_list(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Empty list of items returns empty list."""
        results = await convert_portfolio_items(
            [], target_currency="EUR", fx_service=mock_fx_service
        )
        assert results == []
        mock_fx_service.convert.assert_not_called()

    async def test_zero_amount(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Zero amount converts correctly."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.0945")}, ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("0.00"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )
        assert len(results) == 1
        assert results[0].converted_amount == Decimal("0.00")

    async def test_very_small_amount(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Very small amounts convert correctly with rounding."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.0945")}, ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("0.001"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )
        # 0.001 * 1.0945 = 0.0010945 -> rounded to 0.00
        assert results[0].converted_amount == Decimal("0.00")

    async def test_large_amount(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Large amounts convert correctly without overflow."""
        mock_fx_service.convert.side_effect = _rate_dispatch(
            {"EUR": Decimal("1.0945")}, ts=recent_ts,
        )

        items = [
            _TestItem(amount=Decimal("999999999.99"), currency_code="EUR"),
        ]

        results = await convert_portfolio_items(
            items, target_currency="USD", fx_service=mock_fx_service
        )
        expected = (Decimal("999999999.99") * Decimal("1.0945")).quantize(
            Decimal("0.01"), rounding="ROUND_HALF_UP"
        )
        assert results[0].converted_amount == expected


class TestConvertSingleEdgeCases:
    """Edge case tests for convert_single()."""

    async def test_zero_amount(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Zero amount converts to zero."""
        mock_fx_service.convert.return_value = _make_result(
            from_currency="EUR", to_currency="USD",
            amount=Decimal("0.00"), rate=Decimal("1.0945"),
            ts=recent_ts,
        )
        result = await convert_single(
            amount=Decimal("0.00"), from_currency="EUR",
            to_currency="USD", fx_service=mock_fx_service,
        )
        assert result == Decimal("0.00")

    async def test_negative_amount(
        self, mock_fx_service: MagicMock, recent_ts: datetime
    ) -> None:
        """Negative amount converts correctly."""
        mock_fx_service.convert.return_value = _make_result(
            from_currency="EUR", to_currency="USD",
            amount=Decimal("-100.00"), rate=Decimal("1.0945"),
            ts=recent_ts,
        )
        result = await convert_single(
            amount=Decimal("-100.00"), from_currency="EUR",
            to_currency="USD", fx_service=mock_fx_service,
        )
        assert result == Decimal("-109.45")


# ── convert_currency_rate (indirect-path aware) ─────────────────────────


@pytest.fixture
def mock_fx_service_rates() -> MagicMock:
    """Return a mock FxService that returns None for every get_rate call."""
    svc = MagicMock()
    svc.get_rate = AsyncMock(return_value=None)
    return svc


def _rate_obs(
    base: str,
    quote: str,
    rate: Decimal,
    ts: datetime,
) -> FxRateObservation:
    """Build an FxRateObservation for the mock get_rate to return."""
    return FxRateObservation(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        timestamp=ts,
        source="test_mock",
    )


def _rate_dispatch_get_rate(
    rates: dict[tuple[str, str], Decimal],
    ts: datetime,
) -> Any:
    """Return a side-effect function for get_rate that looks up (base,quote)."""

    async def _side_effect(
        base: str,
        quote: str,
        *,
        at_timestamp: datetime | None = None,
    ) -> FxRateObservation | None:
        key = (base.upper(), quote.upper())
        rate = rates.get(key)
        if rate is None:
            return None
        return _rate_obs(
            base=base.upper(),
            quote=quote.upper(),
            rate=rate,
            ts=at_timestamp or ts,
        )

    return _side_effect


class TestConvertCurrencyRate:
    """Tests for convert_currency_rate() — direct + indirect paths."""

    async def test_same_currency(
        self, mock_fx_service_rates: MagicMock
    ) -> None:
        """Same-currency returns amount unchanged, no fx_service call."""
        result = await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="EUR",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("100.00")
        mock_fx_service_rates.get_rate.assert_not_called()

    async def test_same_currency_quantize(
        self, mock_fx_service_rates: MagicMock
    ) -> None:
        """Same-currency path also rounds the result."""
        result = await convert_currency_rate(
            amount=Decimal("100.999"),
            from_currency="USD",
            to_currency="USD",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("101.00")

    async def test_direct_rate_success(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Direct rate is used when available."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.0945")},
            ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("200.00"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("218.90")

    async def test_indirect_path_via_usd(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """EUR → GBP succeeds via EUR → USD → GBP when EUR→GBP is missing."""
        rates: dict[tuple[str, str], Decimal] = {
            ("EUR", "USD"): Decimal("1.09"),
            ("USD", "GBP"): Decimal("0.79"),
        }
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            rates, ts=recent_ts,
        )

        # Direct EUR→GBP returns None; indirect via USD is used.
        result = await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="GBP",
            fx_service=mock_fx_service_rates,
        )
        # 100 * 1.09 * 0.79 = 86.11
        assert result == Decimal("86.11")

    async def test_indirect_path_second_intermediary(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """When USD fails as intermediary, tries the next one (EUR)."""
        # EUR→JPY is missing directly.
        # USD leg fails (no USD→JPY), but EUR→GBP + GBP→JPY works.
        rates: dict[tuple[str, str], Decimal] = {
            ("EUR", "GBP"): Decimal("0.86"),
            ("GBP", "JPY"): Decimal("191.50"),
        }
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            rates, ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("50.00"),
            from_currency="EUR",
            to_currency="JPY",
            fx_service=mock_fx_service_rates,
        )
        # 50 * 0.86 * 191.50 = 8234.50
        assert result == Decimal("8234.50")

    async def test_indirect_skips_same_currency_intermediary(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Intermediaries that match source or target are skipped."""
        rates: dict[tuple[str, str], Decimal] = {
            ("EUR", "CHF"): Decimal("0.96"),
            ("CHF", "GBP"): Decimal("0.89"),
        }
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            rates, ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="GBP",
            fx_service=mock_fx_service_rates,
        )
        # 100 * 0.96 * 0.89 = 85.44
        assert result == Decimal("85.44")

    async def test_all_paths_exhausted_raises(
        self, mock_fx_service_rates: MagicMock
    ) -> None:
        """When no path exists, raises NoRateError."""
        mock_fx_service_rates.get_rate.return_value = None

        with pytest.raises(NoRateError, match=r"EUR .* JPY"):
            await convert_currency_rate(
                amount=Decimal("100.00"),
                from_currency="EUR",
                to_currency="JPY",
                fx_service=mock_fx_service_rates,
            )

    async def test_forwards_at_timestamp(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """at_timestamp is forwarded to every get_rate call."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.09")},
            ts=recent_ts,
        )

        historical_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="USD",
            at_timestamp=historical_ts,
            fx_service=mock_fx_service_rates,
        )

        # Verify the timestamp was forwarded
        for call in mock_fx_service_rates.get_rate.call_args_list:
            assert call.kwargs.get("at_timestamp") == historical_ts

    async def test_rounding(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Result is rounded to 2 decimal places."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.0945")},
            ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("1.23"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service_rates,
        )
        # 1.23 * 1.0945 = 1.346235 → 1.35
        assert result == Decimal("1.35")

    async def test_indirect_path_rounding(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Indirect path also rounds to 2 decimal places."""
        rates: dict[tuple[str, str], Decimal] = {
            ("EUR", "USD"): Decimal("1.0945"),
            ("USD", "GBP"): Decimal("0.7933"),
        }
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            rates, ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("1.23"),
            from_currency="EUR",
            to_currency="GBP",
            fx_service=mock_fx_service_rates,
        )
        # 1.23 * 1.0945 * 0.7933 = 1.068... → 1.07
        assert result == Decimal("1.07")

    async def test_zero_amount_direct(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Zero amount with direct rate returns zero."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.09")},
            ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("0.00"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("0.00")

    async def test_case_insensitivity(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Lowercase currency codes are normalised."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.09")},
            ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="eur",
            to_currency="usd",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("109.00")

    async def test_leg2_fails_continues_to_next_intermediary(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """When leg1 succeeds but leg2 fails, tries next intermediary.

        Direct EUR→CAD is missing.
        USD intermediary: EUR→USD succeeds, USD→CAD fails (leg2 → continue).
        GBP intermediary: EUR→GBP succeeds, GBP→CAD succeeds → success.
        """
        rates: dict[tuple[str, str], Decimal] = {
            ("EUR", "USD"): Decimal("1.09"),
            ("EUR", "GBP"): Decimal("0.86"),
            ("GBP", "CAD"): Decimal("1.70"),
        }
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            rates, ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("100.00"),
            from_currency="EUR",
            to_currency="CAD",
            fx_service=mock_fx_service_rates,
        )
        # 100 * 0.86 * 1.70 = 146.20
        assert result == Decimal("146.20")

    async def test_negative_amount(
        self, mock_fx_service_rates: MagicMock, recent_ts: datetime
    ) -> None:
        """Negative amount converts correctly with direct rate."""
        mock_fx_service_rates.get_rate.side_effect = _rate_dispatch_get_rate(
            {("EUR", "USD"): Decimal("1.09")},
            ts=recent_ts,
        )

        result = await convert_currency_rate(
            amount=Decimal("-50.00"),
            from_currency="EUR",
            to_currency="USD",
            fx_service=mock_fx_service_rates,
        )
        assert result == Decimal("-54.50")  # -50 * 1.09
