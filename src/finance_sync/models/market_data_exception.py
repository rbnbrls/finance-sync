"""Tenant-scoped, intentional exceptions for unavailable market data."""

from __future__ import annotations

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class MarketDataException(TimestampMixin, Base):
    """A user's decision to accept missing market data for one security."""

    __tablename__ = "market_data_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "security_id", name="uq_market_data_exception"
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
