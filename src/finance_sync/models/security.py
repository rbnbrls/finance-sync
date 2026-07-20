"""Canonical security / instrument model."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import SecurityType
from finance_sync.models.mixins import TimestampMixin


class Security(TimestampMixin, Base):
    """A canonical financial instrument."""

    __tablename__ = "securities"
    __table_args__: ClassVar = (
        UniqueConstraint("isin", name="uq_securities_isin"),
        UniqueConstraint("figi", name="uq_securities_figi"),
        UniqueConstraint("cusip", name="uq_securities_cusip"),
    )

    id: Mapped[str] = pk_uuid()

    isin: Mapped[str | None] = mapped_column(
        String(12), nullable=True, index=True, comment="ISO 6166 ISIN"
    )
    figi: Mapped[str | None] = mapped_column(
        String(12), nullable=True, comment="OpenFIGI identifier"
    )
    cusip: Mapped[str | None] = mapped_column(
        String(9), nullable=True, comment="CUSIP number (US/CA)"
    )
    ticker: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="Popular ticker symbol"
    )
    name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="Canonical instrument name"
    )
    security_type: Mapped[SecurityType] = mapped_column(
        String(64),
        nullable=False,
        comment="stock/etf/mutual_fund/bond/option/crypto/currency/other",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), default="EUR", nullable=False, comment="ISO-4217"
    )

    def __repr__(self) -> str:
        return (
            f"<Security id={self.id!r} isin={self.isin!r} name={self.name!r}>"
        )
