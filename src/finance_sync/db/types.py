"""Custom SQLAlchemy type decorators and type helpers.

Provides reusable :class:`TypeDecorator` subclasses that encode
domain constraints at the database-mapping layer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, String, TypeDecorator

__all__ = [
    "CurrencyCode",
    "MonetaryAmount",
]


class CurrencyCode(TypeDecorator[str]):
    """Stores ISO-4217 currency codes as ``String(3)``.

    Usage::

        currency_code: Mapped[str] = mapped_column(CurrencyCode(3))
    """

    impl = String(3)
    cache_ok = True

    def process_bind_param(  # type: ignore[override]
        self,
        value: str | None,
        _dialect: Any,
    ) -> str | None:
        if value is not None:
            if len(value) != 3 or not value.isalpha():
                msg = (
                    f"Invalid currency code {value!r} — "
                    "must be a 3-letter ISO-4217 code"
                )
                raise ValueError(msg)
            return value.upper()
        return None

    def process_result_value(  # type: ignore[override]
        self,
        value: str | None,
        _dialect: Any,
    ) -> str | None:
        return value


class MonetaryAmount(TypeDecorator[Decimal]):
    """A fixed-point monetary amount stored as ``Numeric(24, 8)``.

    Provides 8 decimal places (sufficient for most FX rates) and up to
    16 integer digits, supporting amounts up to 99,999,999,999,999.99
    (≈ 10¹⁴) in most currencies.
    """

    impl = Numeric(24, 8)
    cache_ok = True

    def process_bind_param(  # type: ignore[override]
        self,
        value: Decimal | float | int | str | None,
        _dialect: Any,
    ) -> Decimal | None:
        if value is not None:
            return Decimal(str(value)).quantize(Decimal("0.00000000"))
        return None

    def process_result_value(  # type: ignore[override]
        self,
        value: Decimal | None,
        _dialect: Any,
    ) -> Decimal | None:
        return value
