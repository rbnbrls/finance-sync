"""Time-series price data model for securities.

Stores OHLCV price observations per security with deduplication
by (security_id, timestamp, source).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy import Text as SA_Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class SecurityPrice(TimestampMixin, Base):
    """A single price observation for a security."""

    __tablename__ = "security_prices"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "security_id",
            "timestamp",
            "source",
            name="uq_security_prices_ts_source",
        ),
    )

    id: Mapped[str] = pk_uuid()

    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="When the price observation was recorded",
    )

    price_open: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Opening price",
    )
    price_high: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Highest price in the period",
    )
    price_low: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Lowest price in the period",
    )
    price_close: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Closing / last price",
    )
    volume: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Trading volume in base units",
    )

    source: Mapped[str] = mapped_column(
        SA_Text,
        nullable=False,
        comment="Data source identifier (e.g. 'openbb', 'manual')",
    )

    interval: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="1d",
        comment="Candle interval: '1m', '5m', '1h', '1d', etc.",
    )

    currency_code: Mapped[str] = mapped_column(
        String(3),
        default="EUR",
        nullable=False,
        comment="ISO-4217 currency code",
    )

    def __repr__(self) -> str:
        return (
            f"<SecurityPrice security_id={self.security_id!r} "
            f"ts={self.timestamp.isoformat()!r} "
            f"close={self.price_close!r}>"
        )
