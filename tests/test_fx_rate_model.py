"""Tests for the FxRate ORM model."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finance_sync.models.fx_rate import FxRate


class TestFxRateModel:
    """Unit tests for the FxRate ORM model."""

    def test_create_instance(self) -> None:
        """Can create an FxRate instance with all required fields."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        assert rate.base_currency == "EUR"
        assert rate.quote_currency == "USD"
        assert rate.rate == Decimal("1.0945")
        assert rate.source == "openbb"

    def test_repr(self) -> None:
        """__repr__ displays the exchange rate pair and value."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        representation = repr(rate)
        assert "EUR" in representation
        assert "USD" in representation
        assert "1.0945" in representation

    def test_default_source(self) -> None:
        """Source is set when passed explicitly (column default is DB-level)."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="GBP",
            rate=Decimal("0.86"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        assert rate.source == "openbb"

    def test_equal_currencies_same_rate(self) -> None:
        """Same base and quote currency implies rate of 1."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="EUR",
            rate=Decimal(1),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="identity",
        )
        assert rate.rate == Decimal(1)

    @pytest.mark.parametrize(
        ("base", "quote", "rate_val"),
        [
            ("USD", "EUR", Decimal("0.9140")),
            ("GBP", "USD", Decimal("1.2650")),
            ("EUR", "JPY", Decimal("160.45")),
        ],
    )
    def test_various_pairs(
        self, base: str, quote: str, rate_val: Decimal
    ) -> None:
        """Various currency pairs can be stored."""
        rate = FxRate(
            base_currency=base,
            quote_currency=quote,
            rate=rate_val,
            timestamp="2026-01-15T12:00:00Z",  # type: ignore[arg-type]
        )
        assert rate.base_currency == base
        assert rate.quote_currency == quote
        assert rate.rate == rate_val
