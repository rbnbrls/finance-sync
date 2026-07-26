"""Time-series exchange rate model for multi-currency support.

Stores exchange rate observations with deduplication
by (base_currency, quote_currency, timestamp, source).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class FxRate(TimestampMixin, Base):
    """A single exchange rate observation between two currencies."""

    __tablename__ = "fx_rates"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "base_currency",
            "quote_currency",
            "timestamp",
            "source",
            name="uq_fx_rates_currencies_ts_source",
        ),
    )

    id: Mapped[str] = pk_uuid()

    base_currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        index=True,
        comment="ISO-4217 base currency code (e.g. 'EUR')",
    )

    quote_currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        index=True,
        comment="ISO-4217 quote currency code (e.g. 'USD')",
    )

    rate: Mapped[Decimal] = mapped_column(
        nullable=False,
        comment="Exchange rate (1 base_currency = rate quote_currency)",
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="When the rate observation was recorded",
    )

    source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="openbb",
        comment="Data source identifier (e.g. 'openbb', 'ecb', 'manual')",
    )

    def __repr__(self) -> str:
        return (
            f"<FxRate {self.base_currency!r}/{self.quote_currency!r} "
            f"={self.rate!r} @ {self.timestamp.isoformat()!r}>"
        )
