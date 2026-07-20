"""Tests for custom SQLAlchemy type decorators."""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_sync.db.types import CurrencyCode, MonetaryAmount


class TestCurrencyCode:
    """Tests for the ISO-4217 currency code type decorator."""

    def test_accepts_valid_code(self) -> None:
        tc = CurrencyCode()
        result = tc.process_bind_param("eur", None)
        assert result == "EUR"

    def test_accepts_uppercase(self) -> None:
        tc = CurrencyCode()
        result = tc.process_bind_param("USD", None)
        assert result == "USD"

    def test_rejects_short_code(self) -> None:
        tc = CurrencyCode()
        with pytest.raises(ValueError, match="Invalid currency code"):
            tc.process_bind_param("EU", None)

    def test_rejects_long_code(self) -> None:
        tc = CurrencyCode()
        with pytest.raises(ValueError, match="Invalid currency code"):
            tc.process_bind_param("EURO", None)

    def test_rejects_numeric(self) -> None:
        tc = CurrencyCode()
        with pytest.raises(ValueError, match="Invalid currency code"):
            tc.process_bind_param("123", None)

    def test_passes_none(self) -> None:
        tc = CurrencyCode()
        assert tc.process_bind_param(None, None) is None

    def test_process_result_value(self) -> None:
        tc = CurrencyCode()
        assert tc.process_result_value("EUR", None) == "EUR"
        assert tc.process_result_value(None, None) is None


class TestMonetaryAmount:
    """Tests for the Numeric(24, 8) monetary amount type decorator."""

    def test_from_decimal(self) -> None:
        ma = MonetaryAmount()
        result = ma.process_bind_param(Decimal("123.45"), None)
        assert result == Decimal("123.45000000")

    def test_from_string(self) -> None:
        ma = MonetaryAmount()
        result = ma.process_bind_param("42.9999", None)
        assert result == Decimal("42.99990000")

    def test_from_int(self) -> None:
        ma = MonetaryAmount()
        result = ma.process_bind_param(100, None)
        assert result == Decimal("100.00000000")

    def test_from_float(self) -> None:
        ma = MonetaryAmount()
        result = ma.process_bind_param(99.99, None)
        assert result == Decimal("99.99000000")

    def test_passes_none(self) -> None:
        ma = MonetaryAmount()
        assert ma.process_bind_param(None, None) is None

    def test_process_result_value(self) -> None:
        ma = MonetaryAmount()
        assert ma.process_result_value(Decimal("123.45"), None) == Decimal(
            "123.45"
        )
        assert ma.process_result_value(None, None) is None
