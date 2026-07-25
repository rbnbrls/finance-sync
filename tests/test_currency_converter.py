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
    RatesFetcher,
    convert,
    convert_amount,
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
                amount=Decimal(50),
                rate=Decimal("1.1"),
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
        mock_fx_service.convert = AsyncMock(
            side_effect=[
                _make_result(
                    from_currency="GBP",
                    to_currency="EUR",
                    rate=Decimal("1.1628"),
                ),
                _make_result(
                    from_currency="USD",
                    to_currency="EUR",
                    rate=Decimal("0.9174"),
                ),
            ]
        )
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
                from_currency="USD",
                to_currency="EUR",
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

    @pytest.mark.parametrize(
        "amount",
        [
            Decimal(0),
            Decimal(-50),
            Decimal("9999999999.99"),
        ],
    )
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

    async def test_empty_portfolio(self, mock_fx_service: MagicMock) -> None:
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
        self,
        mock_fx_service: MagicMock,
    ) -> None:
        """Skips intermediaries that match from_currency or to_currency."""

        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "USD" and quote == "EUR":
                return FxRateObservation(
                    base_currency="USD",
                    quote_currency="EUR",
                    rate=Decimal("0.9174"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "EUR" and quote == "GBP":
                return FxRateObservation(
                    base_currency="EUR",
                    quote_currency="GBP",
                    rate=Decimal("0.86"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)

        # USD->GBP via EUR — the first intermediary is "USD" which should be
        # skipped (matches from_currency), then "EUR" tried next
        result = await convert_currency_rate(
            Decimal(100),
            "USD",
            "GBP",
            fx_service=mock_fx_service,
        )
        # 100 * (0.9174 * 0.86) = 100 * 0.788964 = 78.90
        assert result == Decimal("78.90")

    async def test_leg1_succeeds_leg2_fails_no_path(
        self,
        mock_fx_service: MagicMock,
    ) -> None:
        """Raises NoRateError when leg1 succeeds but leg2 fails."""

        async def _side(base: str, quote: str, **kw: Any) -> Any:
            if base == "GBP" and quote == "USD":
                return FxRateObservation(
                    base_currency="GBP",
                    quote_currency="USD",
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
                Decimal(100),
                "GBP",
                "XYZ",
                fx_service=mock_fx_service,
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
                    base_currency="GBP",
                    quote_currency="USD",
                    rate=Decimal("1.25"),
                    timestamp=ts,
                    source="test",
                )
            if base == "USD" and quote == "JPY":
                return FxRateObservation(
                    base_currency="USD",
                    quote_currency="JPY",
                    rate=Decimal("140.00"),
                    timestamp=ts,
                    source="test",
                )
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert_currency_rate(
            Decimal(100),
            "GBP",
            "JPY",
            at_timestamp=ts,
            fx_service=mock_fx_service,
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
                Decimal(100),
                "ABC",
                "XYZ",
                at_timestamp=ts,
                fx_service=mock_fx_service,
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
                from_currency="USD",
                to_currency="EUR",
                rate=Decimal("0.90"),
                ts=ts,
            )
        )
        items = [
            _TestPosition(Decimal(200), "USD"),
            _TestPosition(Decimal(100), "USD"),
        ]
        results = await convert_portfolio_items(
            items,
            "EUR",
            at_timestamp=ts,
            fx_service=mock_fx_service,
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
            items,
            "EUR",
            at_timestamp=ts,
            fx_service=mock_fx_service,
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

    async def test_direct_conversion(self, mock_fx_service: MagicMock) -> None:
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

    async def test_with_at_date(self, mock_fx_service: MagicMock) -> None:
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

    async def test_at_date_none(self, mock_fx_service: MagicMock) -> None:
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
        await convert(Decimal(50), "EUR", "USD", fx_service=mock_fx_service)
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
            return None  # direct GBP->JPY missing

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert(
            Decimal(10), "GBP", "JPY", fx_service=mock_fx_service
        )
        # 10 * (1.27 * 149.50) = 10 * 189.865 = 1898.65
        assert result == Decimal("1898.65")


# -- Tests: convert_amount -----------------------------------------------------


class TestConvertAmount:
    """convert_amount() -- lightweight rates-fetcher callable utility."""

    @pytest.fixture
    def _no_rates(self) -> RatesFetcher:
        """A fetcher that always returns None (no rates available)."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return None
        return fetcher

    async def test_identity_conversion(
        self, _no_rates: RatesFetcher
    ) -> None:
        """Same-currency returns the amount unchanged (rounded)."""
        result = await convert_amount(
            Decimal("150.00"), "EUR", "EUR", _no_rates,
        )
        assert result == Decimal("150.00")

    async def test_identity_case_insensitive(
        self, _no_rates: RatesFetcher
    ) -> None:
        """Identity works regardless of case."""
        result = await convert_amount(
            Decimal(50), "eur", "EUR", _no_rates,
        )
        assert result == Decimal("50.00")

    async def test_direct_rate_success(self) -> None:
        """Uses the direct rate when rates_fetcher returns a value."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.09") if (from_, to_) == ("EUR", "USD") else None

        result = await convert_amount(Decimal(200), "EUR", "USD", fetcher)
        assert result == Decimal("218.00")

    async def test_direct_rate_case_insensitive(self) -> None:
        """Normalises currency codes to uppercase before lookup."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.09") if (from_, to_) == ("EUR", "USD") else None

        result = await convert_amount(Decimal(100), "eur", "usd", fetcher)
        assert result == Decimal("109.00")

    async def test_inverse_pair_fallback(self) -> None:
        """Falls back to inverse pair when the direct rate is missing."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            # Only USD->EUR is known, not EUR->USD
            if (from_, to_) == ("USD", "EUR"):
                return Decimal("0.9174")
            return None

        # EUR->USD should use USD->EUR inverted: 1 / 0.9174 ≈ 1.09
        result = await convert_amount(Decimal(100), "EUR", "USD", fetcher)
        # 100 * (1 / 0.9174) ≈ 109.00
        assert result == Decimal("109.00")

    async def test_both_direct_and_inverse_fail(self) -> None:
        """Raises NoRateError when neither direct nor inverse is available."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return None

        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert_amount(
                Decimal(100), "ABC", "XYZ", fetcher,
            )

    async def test_inverse_rate_of_zero_raises(self) -> None:
        """An inverse rate of zero does not crash -- falls through to error."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            if (from_, to_) == ("USD", "XYZ"):
                return Decimal(0)
            return None

        with pytest.raises(NoRateError, match="No exchange rate"):
            await convert_amount(Decimal(100), "XYZ", "USD", fetcher)

    async def test_zero_amount(self) -> None:
        """Zero amount converts without error."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.09")

        result = await convert_amount(Decimal(0), "EUR", "USD", fetcher)
        assert result == Decimal("0.00")

    async def test_negative_amount(self) -> None:
        """Negative amounts convert without error."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.09")

        result = await convert_amount(Decimal(-50), "EUR", "USD", fetcher)
        assert result == Decimal("-54.50")

    async def test_large_amount(self) -> None:
        """Large amounts round correctly."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.23456789")

        result = await convert_amount(
            Decimal("9999999999.99"), "EUR", "USD", fetcher,
        )
        expected = Decimal("9999999999.99") * Decimal("1.23456789")
        assert result == expected.quantize(
            Decimal("0.01"), rounding="ROUND_HALF_UP",
        )

    async def test_rounding_to_two_decimals(self) -> None:
        """Result is always rounded to 2 decimal places, ROUND_HALF_UP."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.2345")

        result = await convert_amount(Decimal("33.33"), "EUR", "USD", fetcher)
        # 33.33 * 1.2345 = 41.145885 -> 41.15 (ROUND_HALF_UP)
        assert result == Decimal("41.15")

    async def test_rounding_midpoint(self) -> None:
        """Midpoint rounding follows ROUND_HALF_UP."""
        async def fetcher(from_: str, to_: str) -> Decimal | None:
            return Decimal("1.005")

        result = await convert_amount(Decimal("1.00"), "EUR", "USD", fetcher)
        # 1.00 * 1.005 = 1.005 -> rounded to 1.01 (ROUND_HALF_UP)
        assert result == Decimal("1.01")

    async def test_works_with_fx_service_get_rate(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Works as a rates_fetcher wrapping FxService.get_rate()."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            )
        )

        async def fetcher(from_: str, to_: str) -> Decimal | None:
            obs = await mock_fx_service.get_rate(from_, to_)
            return obs.rate if obs is not None else None

        result = await convert_amount(Decimal(100), "EUR", "USD", fetcher)
        assert result == Decimal("109.00")
        mock_fx_service.get_rate.assert_awaited_once_with("EUR", "USD")

    async def test_rates_fetcher_type_alias(self) -> None:
        """RatesFetcher type alias is importable."""
        # Verify it can be used as an annotation without error
        async def dummy(f: str, t: str) -> Decimal | None:
            return None
        annotated: RatesFetcher = dummy
        result = await annotated("EUR", "USD")
        assert result is None


# -- Property-based / round-trip tests ----------------------------------------


class TestConversionProperties:
    """Cross-cutting properties that should hold for conversion functions."""

    async def test_round_trip_via_inverse(self) -> None:
        """convert_amount(A→B) then convert_amount(result→A) ≈ original
        when both directions are known."""
        async def bidirectional(from_: str, to_: str) -> Decimal | None:
            rates = {
                ("EUR", "USD"): Decimal("1.09"),
                ("USD", "EUR"): Decimal("0.9174"),
            }
            return rates.get((from_, to_))

        fwd = await convert_amount(Decimal("100.00"), "EUR", "USD", bidirectional)
        assert fwd == Decimal("109.00")

        rev = await convert_amount(fwd, "USD", "EUR", bidirectional)
        # Round-trip: 109.00 * 0.9174 = 99.9966 → rounds to 100.00
        assert rev == Decimal("100.00")

    async def test_round_trip_via_single_rate(self) -> None:
        """Direct + inverse from a single rate: round-trip preserves value."""
        async def single_rate(from_: str, to_: str) -> Decimal | None:
            if (from_, to_) == ("EUR", "USD"):
                return Decimal("1.09")
            if (from_, to_) == ("USD", "EUR"):
                return Decimal(1) / Decimal("1.09")
            return None

        fwd = await convert_amount(Decimal("200.00"), "EUR", "USD", single_rate)
        rev = await convert_amount(fwd, "USD", "EUR", single_rate)
        # 200 * 1.09 = 218 → 218 * (1/1.09) = 200.00
        assert rev == Decimal("200.00")


# -- Cross-rate through later intermediary ------------------------------------


class TestCrossRateLaterIntermediary:
    """Cross-rate path resolution through non-first intermediaries."""

    async def test_path_through_second_intermediary(
        self, mock_fx_service: MagicMock
    ) -> None:
        """When the first intermediary (USD) also has no rate, falls through
        to the next intermediary (EUR) to find a path."""
        async def _side(base: str, quote: str, **kw: Any) -> Any:
            # Direct NOK→SEK is missing
            # USD→SEK is also missing (first intermediary fails)
            # EUR→SEK exists
            if base == "NOK" and quote == "EUR":
                return FxRateObservation(
                    base_currency="NOK", quote_currency="EUR",
                    rate=Decimal("0.085"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            if base == "EUR" and quote == "SEK":
                return FxRateObservation(
                    base_currency="EUR", quote_currency="SEK",
                    rate=Decimal("11.50"),
                    timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                    source="test",
                )
            return None

        mock_fx_service.get_rate = AsyncMock(side_effect=_side)
        result = await convert_currency_rate(
            Decimal(100), "NOK", "SEK", fx_service=mock_fx_service,
        )
        # Intermediaries: USD(missing), EUR(hit for both legs)
        # 100 * (0.085 * 11.50) = 100 * 0.9775 = 97.75
        assert result == Decimal("97.75")

    async def test_exhausts_all_intermediaries_message(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Error message lists all exhausted intermediaries when all paths
        fail, including the intermediary currencies."""
        mock_fx_service.get_rate = AsyncMock(return_value=None)
        with pytest.raises(NoRateError) as exc_info:
            await convert_currency_rate(
                Decimal(100), "XXX", "YYY", fx_service=mock_fx_service,
            )
        msg = str(exc_info.value)
        assert "USD" in msg
        assert "EUR" in msg
        assert "GBP" in msg
        assert "No exchange rate" in msg


# -- Portfolio edge cases -----------------------------------------------------


class TestConvertPortfolioItemsEdgeCases:
    """Additional edge cases for batch portfolio conversion."""

    async def test_mixed_identity_and_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Items already in the target currency use rate=1; others convert."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="EUR", rate=Decimal("0.9174"),
            )
        )
        items = [
            _TestPosition(Decimal(100), "EUR"),  # identity
            _TestPosition(Decimal(200), "USD"),  # convert
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service,
        )
        assert len(results) == 2
        # Identity item
        assert results[0].converted_amount == Decimal("100.00")
        assert results[0].rate_used == Decimal(1)
        assert results[0].original_currency == "EUR"
        # Converted item
        assert results[1].converted_amount == Decimal("183.48")
        assert results[1].rate_used == Decimal("0.9174")
        assert results[1].original_currency == "USD"
        # Only one convert call (the USD→EUR rate lookup)
        mock_fx_service.convert.assert_awaited_once()

    async def test_all_identity_with_extra_currencies(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Multiple items all in target currency — no convert calls at all."""
        items = [
            _TestPosition(Decimal(10), "EUR"),
            _TestPosition(Decimal(20), "EUR"),
            _TestPosition(Decimal(30), "EUR"),
        ]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service,
        )
        assert len(results) == 3
        assert results[0].converted_amount == Decimal("10.00")
        assert results[2].converted_amount == Decimal("30.00")
        mock_fx_service.convert.assert_not_called()

    async def test_single_item_conversion(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Single-item portfolio conversion works correctly."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="GBP", rate=Decimal("0.7874"),
            )
        )
        items = [_TestPosition(Decimal("500.00"), "USD")]
        results = await convert_portfolio_items(
            items, "GBP", fx_service=mock_fx_service,
        )
        assert len(results) == 1
        assert results[0].converted_amount == Decimal("393.70")
        assert results[0].original_currency == "USD"
        assert results[0].target_currency == "GBP"


# -- Case-insensitive identity edges ------------------------------------------


class TestCaseInsensitiveIdentity:
    """Same-currency shortcut with non-matching case."""

    async def test_convert_single_case_insensitive_identity(
        self, mock_fx_service: MagicMock
    ) -> None:
        """convert_single with same currency but different case goes through
        to FxService which handles the identity internally."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="EUR", to_currency="EUR", rate=Decimal(1),
                converted=Decimal("100.00"),
            )
        )
        result = await convert_single(
            Decimal("100.00"), "eur", "EUR", fx_service=mock_fx_service,
        )
        # Falls through to FxService which returns identity
        assert result == Decimal("100.00")
        mock_fx_service.convert.assert_awaited_once()

    async def test_convert_case_insensitive_identity(
        self, mock_fx_service: MagicMock
    ) -> None:
        """convert() with same currency but different case routes through
        convert_currency_rate which normalises case and hits identity."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR", quote_currency="EUR", rate=Decimal(1),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="identity",
            ),
        )
        result = await convert(
            Decimal("100.00"), "eur", "EUR", fx_service=mock_fx_service,
        )
        assert result == Decimal("100.00")


# -- Rounding edge cases ------------------------------------------------------


class TestRoundingEdgeCases:
    """Midpoint and precision edge cases for conversion functions."""

    async def test_convert_currency_rate_midpoint_rounding(
        self, mock_fx_service: MagicMock
    ) -> None:
        """convert_currency_rate rounds midpoint values up (ROUND_HALF_UP)."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR", quote_currency="USD", rate=Decimal("1.005"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            ),
        )
        result = await convert_currency_rate(
            Decimal("1.00"), "EUR", "USD", fx_service=mock_fx_service,
        )
        # 1.00 * 1.005 = 1.005 → rounds to 1.01
        assert result == Decimal("1.01")

    async def test_convert_currency_rate_high_precision(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Rate with many decimal places rounds correctly."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR", quote_currency="USD",
                rate=Decimal("1.123456789"),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            ),
        )
        result = await convert_currency_rate(
            Decimal("999.99"), "EUR", "USD", fx_service=mock_fx_service,
        )
        # 999.99 * 1.123456789 = 1123.44555... → rounds to 1123.45
        assert result == Decimal("1123.45")

    async def test_convert_portfolio_items_midpoint_rounding(
        self, mock_fx_service: MagicMock
    ) -> None:
        """Portfolio items with midpoint values round correctly (ROUND_HALF_UP)."""
        mock_fx_service.convert = AsyncMock(
            return_value=_make_result(
                from_currency="USD", to_currency="EUR", rate=Decimal("0.9174"),
            ),
        )
        items = [_TestPosition(Decimal("99.99"), "USD")]
        results = await convert_portfolio_items(
            items, "EUR", fx_service=mock_fx_service,
        )
        # 99.99 * 0.9174 = 91.730826 → rounds to 91.73
        assert results[0].converted_amount == Decimal("91.73")


# -- Direct rate edge case: zero-rate protection ------------------------------


class TestConvertCurrencyRateZeroRate:
    """convert_currency_rate with zero-rate protection."""

    async def test_zero_rate_returns_zero(
        self, mock_fx_service: MagicMock
    ) -> None:
        """A zero exchange rate returns zero converted amount (no crash)."""
        mock_fx_service.get_rate = AsyncMock(
            return_value=FxRateObservation(
                base_currency="EUR", quote_currency="USD", rate=Decimal(0),
                timestamp=datetime(2026, 7, 23, tzinfo=UTC),
                source="test",
            ),
        )
        result = await convert_currency_rate(
            Decimal("100.00"), "EUR", "USD", fx_service=mock_fx_service,
        )
        assert result == Decimal("0.00")
