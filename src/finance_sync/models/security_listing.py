"""Tradable venue / exchange listing model."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class SecurityListing(TimestampMixin, Base):
    """A security listed on a specific exchange / venue.

    One ``Security`` may have many listings (e.g. same ISIN traded on
    Xetra, Euronext, NYSE with different tickers and currencies).
    """

    __tablename__ = "security_listings"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "security_id", "mic", "currency_code", name="uq_listings_venue_ccy"
        ),
    )

    id: Mapped[str] = pk_uuid()
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Venue ────────────────────────────────────────────────────────
    mic: Mapped[str] = mapped_column(
        String(4),
        nullable=False,
        comment="ISO 10383 Market Identifier Code (e.g. 'XAMS', 'XNYS')",
    )
    exchange_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    # ── Listing details ──────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Ticker at this venue"
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="ISO-4217"
    )
    is_primary_listing: Mapped[bool] = mapped_column(
        default=False, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<SecurityListing ticker={self.ticker!r} "
            f"mic={self.mic!r} security_id={self.security_id!r}>"
        )
