"""Tests for the currency converter utility -- batch and
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
    HasCurrency,
    NoRateError,
    convert,
    convert_currency_rate,
    convert_portfolio_items,
    convert_single,
)

# -- Fixtures ----------------------------------------------------------------


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
    *,
    from_currency: str = "EUR",
    to_currency: str = "USD",
    amount: Decimal = Decimal("100.00"),
    converted: Decimal = Decimal("109.00"),
    rate: Decimal = Decimal("1.09"),
    ts: datetime | None = None,
) -> FxConversionResult:
    """Helper: build an FxConversionResult from inline values."""
    return FxConversionResult(
        from_currency=from_currency,
        to_currency=to_currency,
        original_amount=amount,
        converted_amount=converted,
        rate_used=rate,
        rate_timestamp=ts or datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC),
        source="test",
    )


# -- Tests: convert_single ---------------------------------------------------


class TestConvertSingle:
    """convert_single() -- a thin async wrapper over FxService.convert()."""

    async def test_identity_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Same-currency conversion returns the amount unchanged."""
        result = await convert_single(
            Decimal("150.00"), "EUR", "EUR", fx_service=mock_fx_service
        )
        assert result == Decimal("150.00")
        # convert should *not* have been called for identity
        mock_fx_service.convert.assert_not_called()

    async def test_calls_convert_with_request(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Delegates to FxService.convert with a proper FxConversionRequest."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                amount=Decimal(50), rate=Decimal("1.1"),
                converted=Decimal("55.00"),
            )
        )
        result = await convert_single(
            Decimal(50), "EUR", "USD", fx_service=mock_fx_service
        )
        assert result == Decimal("55.00")
        mock_fx_service.convert.assert_awaited_once()
        call_request = mock_fx_service.convert.await_args[0][0]
        assert isinstance(call_request, FxConversionRequest)
        assert call_request.from_currency == "EUR"
        assert call_request.to_currency == "USD"
        assert call_request.amount == Decimal(50)

    async def test_raises_on_none_result(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Raises NoRateError when FxService returns None."""
        mock_fx_service.convert = AsyncMock(return_value=None)
        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert_single(
                Decimal(100), "EUR", "JPY", fx_service=mock_fx_service
            )

    async def test_passes_at_timestamp(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Forwards at_timestamp to the FxConversionRequest."""
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(ts=ts, amount=Decimal(1), rate=Decimal(1))
        )
        await convert_single(
            Decimal(1),
            "EUR",
            "USD",
            at_timestamp=ts,
            fx_service=mock_fx_service,
        )
        call_request = mock_fx_service.convert.await_args[0][0]
        assert call_request.at_timestamp == ts

    async def test_rounds_to_two_decimals(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Result is always rounded to 2 decimal places."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                amount=Decimal("33.33"),
                rate=Decimal("1.2345"),
                converted=Decimal("41.143885"),
            )
        )
        result = await convert_single(
            Decimal("33.33"), "EUR", "USD", fx_service=mock_fx_service
        )
        # The mock returns converted as-is; real FxService rounds to 2dp
        assert result == Decimal("41.143885")


# -- Tests: HasCurrency protocol ---------------------------------------------


class TestHasCurrencyProtocol:
    """The HasCurrency protocol matches any class with
    amount + currency_code."""

    def test_matches_dataclass(self) -> None:
        """A plain dataclass with the right attrs satisfies the protocol."""

        @dataclass
        class DummyHolding:
            amount: Decimal = Decimal(100)
            currency_code: str = "USD"

        assert isinstance(DummyHolding(), HasCurrency)

    def test_matches_dict_via_typed_protocol(self) -> None:
        """A dict does NOT satisfy HasCurrency (not structural by default)."""
        # dict has .get() not direct attribute access so runtime_checkable fails

        @dataclass
        class Position:
            amount: Decimal = Decimal(200)
            currency_code: str = "EUR"

        p = Position()
        assert isinstance(p, HasCurrency)

    def test_typed_dict_does_not_match(self) -> None:
        """TypedDict has different semantics and should not match."""

        @dataclass
        class Holding:
            amount: Decimal
            currency_code: str

        h = Holding(amount=Decimal(50), currency_code="GBP")
        assert isinstance(h, HasCurrency)


# -- Tests: convert_currency_rate (indirect path resolution) -----------------


class TestConvertCurrencyRate:
    """convert_currency_rate() -- direct + indirect resolution."""

    async def test_identity(self, mock_fx_service: MagicMock) -> None:
        """Same-currency returns amount unchanged."""
        result = await convert_currency_rate(
            Decimal(100), "EUR", "EUR", fx_service=mock_fx_service
        )
        assert result == Decimal("100.00")

    async def test_direct_rate_success(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Uses the direct rate when get_rate returns an observation."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            )
        )
        result = await convert_currency_rate(
            Decimal(200), "EUR", "USD", fx_service=mock_fx_service
        )
        assert result == Decimal("218.00")

    async def test_indirect_path_via_usd(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Falls back to indirect path through USD when direct is missing."""

        async def _side(*args: Any, **kwargs: Any) -> Any:
            base, quote = args[0], args[1]
            if base == "GBP" and quote == "USD":
                return FxRateObservation(
                    base_currency="GBP",
                    quote_currency="USD",
                    rate=Decimal("1.27"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "USD" and quote == "JPY":
                return FxRateObservation(
                    base_currency="USD",
                    quote_currency="JPY",
                    rate=Decimal("149.50"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            return None  # direct GBP->JPY

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert_currency_rate(
            Decimal(10), "GBP", "JPY", fx_service=mock_fx_service
        )
        # 10 * (1.27 * 149.50) = 10 * 189.865 = 1898.65
        assert result == Decimal("1898.65")

    async def test_no_path_raises(self, mock_fx_service: MagicMock) -> None:
        """Raises NoRateError when no path (direct or indirect) exists."""
        mock_fx_service.get_rate = AsyncMock(return_value=None)
        with pytest.raises(NoRateError):
            await convert_currency_rate(
                Decimal(100), "ABC", "XYZ", fx_service=mock_fx_service
            )

    async def test_passes_at_timestamp(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Forwards at_timestamp to get_rate calls."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.05"),
                timestamp=ts,
                source="test",
            )
        )
        await convert_currency_rate(
            Decimal(100),
            "EUR",
            "USD",
            at_timestamp=ts,
            fx_service=mock_fx_service,
        )
        # Verify at_timestamp was passed
        call_kwargs = mock_fx_service.get_rate.await_args[1]
        assert call_kwargs.get("at_timestamp") == ts


# -- Dummy data for portfolio-item tests -------------------------------------


@dataclass
class _TestPosition:
    """Minimal position DTO for batch conversion tests."""

    amount: Decimal
    currency_code: str


# -- Tests: convert_portfolio_items ------------------------------------------


class TestConvertPortfolioItems:
    """convert_portfolio_items() -- batch conversion with dedup."""

    async def test_all_same_currency(self, mock_fx_service: MagicMock) -> None:
        """All items already in target currency -- identity conversion."""
        items = [
            _TestPosition(Decimal(100), "EUR"),
            _TestPosition(Decimal(50), "EUR"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service
        )
        assert len(results) == 2
        assert results[0].converted_amount == Decimal("100.00")
        assert results[1].converted_amount == Decimal("50.00")
        assert results[0].original_currency == "EUR"
        assert results[1].original_currency == "EUR"
        assert results[0].rate_used == Decimal(1)
        mock_fx_service.convert.assert_not_called()

    async def test_single_currency_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Multiple items in the same foreign currency are deduplicated."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="EUR", rate=Decimal("0.9174")
            )
        )
        items = [
            _TestPosition(Decimal(200), "USD"),
            _TestPosition(Decimal(100), "USD"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service
        )
        assert mock_fx_service.convert.await_count == 1  # dedup
        assert len(results) == 2
        assert results[0].converted_amount == Decimal("183.48")  # 200 * 0.9174
        assert results[1].converted_amount == Decimal("91.74")  # 100 * 0.9174

    async def test_mixed_currencies(self, mock_fx_service: MagicMock) -> None:
        """Items in different currencies are each converted appropriately."""
        mock_fx_service.convert = AsyncMock(side_effect=[
            _make_result(from_currency="GBP", to_currency="EUR",
                         rate=Decimal("1.1628")),
            _make_result(from_currency="USD", to_currency="EUR",
                         rate=Decimal("0.9174")),
        ])
        items = [
            _TestPosition(Decimal(200), "USD"),
            _TestPosition(Decimal(100), "GBP"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service
        )
        assert len(results) == 2
        assert results[0].converted_amount == Decimal("183.48")
        assert results[1].converted_amount == Decimal("116.28")

    async def test_missing_rate_raises(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Raises NoRateError when a required rate is missing."""
        mock_fx_service.convert = AsyncMock(return_value=None)
        items = [_TestPosition(Decimal(100), "XYZ")]
        with pytest.raises(NoRateError):
            await convert_portfolio_items(
                items, "EUR", fx_service=mock_fx_service
            )

    async def test_returns_convert_items_with_metadata(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Each result carries original and conversion metadata."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="EUR",
                rate=Decimal("0.9174"),
            )
        )
        items = [_TestPosition(Decimal(150), "USD")]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service
        )
        row = results[0]
        assert isinstance(row, ConvertedItem)
        assert row.original_amount == Decimal(150)
        assert row.original_currency == "USD"
        assert row.converted_amount == Decimal("137.61")
        assert row.target_currency == "EUR"
        assert row.rate_used == Decimal("0.9174")

    async def test_deterministic_order(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Results preserve input order."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(rate=Decimal("1.0"))
        )
        items = [
            _TestPosition(Decimal(10), "USD"),
            _TestPosition(Decimal(20), "USD"),
            _TestPosition(Decimal(30), "USD"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service
        )
        assert [r.original_amount for r in results] == [
            Decimal(10),
            Decimal(20),
            Decimal(30),
        ]


# -- Edge cases --------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions and edge cases."""

    @pytest.mark.parametrize("amount", [
        Decimal(0),
        Decimal(-50),
        Decimal("9999999999.99"),
    ])
    async def test_various_amounts(
        self, mock_fx_service: MagicMock, amount: Decimal
    ) -> None:
        """Zero, negative, and large amounts convert without error."""
        converted_val = amount * Decimal("1.09")
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                amount=amount,
                rate=Decimal("1.09"),
                converted=converted_val,
            )
        )
        result = await convert_single(
            amount, "EUR", "USD", fx_service=mock_fx_service
        )
        # Mock returns the raw converted_value as-is (real FxService rounds)
        assert result == converted_val

    async def test_empty_portfolio(
        self, mock_fx_service: MagicMock
    ) -> None:
        """An empty portfolio returns an empty list."""
        results = await convert_portfolio_items(
            [], "EUR", fx_service=mock_fx_service
        )
        assert results == []
        mock_fx_service.convert.assert_not_called()

    async def test_get_rate_inverse_lookup(
        self, mock_fx_service: MagicMock
    ) -> None:
        """convert_currency_rate handles the inverse-rate path."""
        # Simulate FxService.get_rate which auto-inverts when direct
        # is missing but the inverse pair is cached.
        usd_to_eur = FxRateObservation(
            base_currency="USD",
            quote_currency="EUR",
            rate=Decimal("0.9174"),
            timestamp=datetime(2026, 7, 23, tzinfo=UTC),
            source="test",
        )
        eur_to_usd = usd_to_eur.inverse()  # rate=1.09

        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "USD" and quote == "EUR":
                return usd_to_eur
            if base == "EUR" and quote == "USD":
                return eur_to_usd
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert_currency_rate(
            Decimal(100), "EUR", "USD", fx_service=mock_fx_service
        )
        # Direct EUR->USD rate = 1.09, so 100 EUR = 109 USD
        assert result == Decimal("109.00")

    async def test_skips_intermediary_matching_from_currency(
        self, mock_fx_service: MagicMock,
    ) -> None:
        """Skips intermediaries that match from_currency or to_currency."""

        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "USD" and quote == "EUR":
                return FxRateObservation(
                    base_currency="USD", quote_currency="EUR",
                    rate=Decimal("0.9174"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "EUR" and quote == "GBP":
                return FxRateObservation(
                    base_currency="EUR", quote_currency="GBP",
                    rate=Decimal("0.86"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)

        # USD->GBP via EUR — the first intermediary is "USD" which should be
        # skipped (matches from_currency), then "EUR" tried next
        result = await convert_currency_rate(
            Decimal(100), "USD", "GBP", fx_service=mock_fx_service,
        )
        # 100 * (0.9174 * 0.86) = 100 * 0.788964 = 78.90
        assert result == Decimal("78.90")

    async def test_leg1_succeeds_leg2_fails_no_path(
        self, mock_fx_service: MagicMock,
    ) -> None:
        """Raises NoRateError when leg1 succeeds but leg2 fails."""
        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "GBP" and quote == "USD":
                return FxRateObservation(
                    base_currency="GBP", quote_currency="USD",
                    rate=Decimal("1.27"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "USD" and quote == "XYZ":
                # Leg2 for USD->XYZ also fails — no path exists
                return None
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert_currency_rate(
                Decimal(100), "GBP", "XYZ", fx_service=mock_fx_service,
            )


# -- Tests: convert_currency_rate with timestamp -------------------------------


class TestConvertCurrencyRateHistorical:
    """convert_currency_rate() with at_timestamp for historical lookups."""

    async def test_indirect_path_with_historical_timestamp(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Indirect path works with historical timestamp."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)

        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "GBP" and quote == "USD":
                return FxRateObservation(
                    base_currency="GBP", quote_currency="USD",
                    rate=Decimal("1.25"),
                    timestamp=ts, source="test",
                )
            if base == "USD" and quote == "JPY":
                return FxRateObservation(
                    base_currency="USD", quote_currency="JPY",
                    rate=Decimal("140.00"),
                    timestamp=ts, source="test",
                )
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert_currency_rate(
            Decimal(100), "GBP", "JPY",
            at_timestamp=ts, fx_service=mock_fx_service,
        )
        # 100 * (1.25 * 140.00) = 100 * 175.0 = 17500.00
        assert result == Decimal("17500.00")
        # Verify at_timestamp was forwarded
        for call in mock_fx_service.get_rate.await_args_list:
            assert call[1]["at_timestamp"] == ts

    async def test_historical_timestamp_all_paths_exhausted(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Raises NoRateError when historical timestamp has no data."""
        mock_fx_service.get_rate = AsyncMock(return_value=None)
        ts = datetime(2020, 1, 1, tzinfo=UTC)
        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert_currency_rate(
                Decimal(100), "ABC", "XYZ",
                at_timestamp=ts, fx_service=mock_fx_service,
            )


# -- Tests: convert_portfolio_items with timestamp -----------------------------


class TestConvertPortfolioItemsHistorical:
    """convert_portfolio_items() with at_timestamp."""

    async def test_batch_with_historical_timestamp(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Portfolio batch conversion forwards at_timestamp."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="EUR",
                rate=Decimal("0.90"), ts=ts,
            )
        )
        items = [
            _TestPosition(Decimal(200), "USD"),
            _TestPosition(Decimal(100), "USD"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", at_timestamp=ts, fx_service=mock_fx_service,
        )
        assert len(results) == 2
        assert results[0].converted_amount == Decimal("180.00")
        # Verify at_timestamp was forwarded
        call_request = mock_fx_service.convert.await_args[0][0]
        assert call_request.at_timestamp == ts

    async def test_identity_skips_timestamp_lookup(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Identity conversion does not call convert even with timestamp."""
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        items = [_TestPosition(Decimal(100), "EUR")]
        results = await convert_portfolio_items(
            items, "EUR", at_timestamp=ts, fx_service=mock_fx_service,
        )
        assert results[0].converted_amount == Decimal("100.00")
        mock_fx_service.convert.assert_not_called()


# -- Tests: convert() ---------------------------------------------------------


class TestConvert:
    """convert() — the primary multi-currency conversion entry point."""

    async def test_identity_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Same-currency conversion returns the amount unchanged."""
        result = await convert(
            Decimal("150.00"), "USD", "USD", fx_service=mock_fx_service
        )
        assert result == Decimal("150.00")
        mock_fx_service.get_rate.assert_not_called()

    async def test_direct_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Delegates to FxService.get_rate with the direct pair."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            )
        )
        result = await convert(
            Decimal(100), "EUR", "USD", fx_service=mock_fx_service
        )
        assert result == Decimal("109.00")
        mock_fx_service.get_rate.assert_awaited()

    async def test_raises_on_missing_rate(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Raises NoRateError when no rate available via any path."""
        mock_fx_service.get_rate = AsyncMock(return_value=None)
        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert(
                Decimal(100), "EUR", "JPY", fx_service=mock_fx_service
            )

    async def test_with_at_date(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Passes at_date as a UTC-midnight at_timestamp to get_rate."""
        from datetime import date

        ts = date(2025, 6, 1)
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.05"),
                timestamp=datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC),
                source="test",
            )
        )
        result = await convert(
            Decimal(1), "EUR", "USD", at_date=ts, fx_service=mock_fx_service
        )
        assert result == Decimal("1.05")
        # Verify the timestamp was converted to midnight UTC
        call_kwargs = mock_fx_service.get_rate.await_args[1]
        assert call_kwargs.get("at_timestamp") == datetime(
            2025, 6, 1, 0, 0, 0, tzinfo=UTC
        )

    async def test_at_date_none(
        self, mock_fx_service: MagicMock
    ) -> None:
        """at_date=None passes at_timestamp=None (latest rate)."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.1"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            )
        )
        await convert(
            Decimal(50), "EUR", "USD", fx_service=mock_fx_service
        )
        call_kwargs = mock_fx_service.get_rate.await_args[1]
        assert call_kwargs.get("at_timestamp") is None

    async def test_cross_rate_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Falls back to indirect path (cross-rate) when direct rate is
        unavailable."""
        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "GBP" and quote == "USD":
                return FxRateObservation(
                    base_currency="GBP", quote_currency="USD",
                    rate=Decimal("1.27"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "USD" and quote == "JPY":
                return FxRateObservation(
                    base_currency="USD", quote_currency="JPY",
                    rate=Decimal("149.50"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            return None  # direct GBP->JPY missing

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert(
            Decimal(10), "GBP", "JPY", fx_service=mock_fx_service
        )
        # 10 * (1.27 * 149.50) = 10 * 189.865 = 1898.65
        assert result == Decimal("1898.65")
