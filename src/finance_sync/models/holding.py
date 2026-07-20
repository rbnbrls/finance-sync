"""Time-versioned position snapshot model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import HoldingSource
from finance_sync.models.mixins import TimestampMixin


class Holding(TimestampMixin, Base):
    """A point-in-time snapshot of a position in one security."""

    __tablename__ = "holdings"

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When this snapshot was observed / reported",
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="Number of units held"
    )
    cost_basis: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="Total cost basis"
    )
    cost_basis_currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217"
    )
    market_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Market value at observation time",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="ISO-4217 for quantity/market_value"
    )
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="Unit price at observation time"
    )
    price_currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217 for price"
    )

    source: Mapped[HoldingSource] = mapped_column(
        String(64),
        nullable=False,
        comment="'provider_sync', 'computed', 'manual_adjustment'",
    )

    def __repr__(self) -> str:
        return (
            f"<Holding id={self.id!r} account={self.account_id!r} "
            f"security={self.security_id!r} qty={self.quantity!r}>"
        )
