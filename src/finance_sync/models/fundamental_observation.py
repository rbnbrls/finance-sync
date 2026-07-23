"""Fundamental metrics and ratios for securities.

Stores point-in-time observations of fundamental financial data
such as PE ratios, EPS, market cap, dividend yield, and beta.
Deduplicated by (security_id, timestamp, source).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class FundamentalObservation(TimestampMixin, Base):
    """A point-in-time observation of fundamental metrics for a security.

    Stores common fundamental ratios and financial metrics sourced from
    OpenBB or other data providers.  Each row captures a snapshot of
    fundamental data at a given time.
    """

    __tablename__ = "fundamental_observations"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "security_id",
            "timestamp",
            "source",
            name="uq_fundamental_obs_ts_source",
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
        comment="When the fundamental observation was recorded",
    )

    # ── Valuation ratios ────────────────────────────────────────────
    pe_ratio: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Price-to-Earnings ratio (trailing twelve months)",
    )
    forward_pe: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Forward Price-to-Earnings ratio",
    )
    peg_ratio: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="PE / Growth ratio",
    )

    # ── Per-share metrics ───────────────────────────────────────────
    eps: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Earnings Per Share (trailing twelve months)",
    )
    eps_forward: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Forward Earnings Per Share estimate",
    )
    book_value_per_share: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Book Value Per Share",
    )

    # ── Dividend ────────────────────────────────────────────────────
    dividend_yield: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Dividend yield as a decimal (e.g. 0.035 = 3.5%)",
    )
    dividend_rate: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Annual dividend rate per share",
    )

    # ── Size & liquidity ────────────────────────────────────────────
    market_cap: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Market capitalisation in base currency",
    )
    enterprise_value: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Enterprise value",
    )
    shares_outstanding: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Number of shares outstanding",
    )

    # ── Risk & volatility ───────────────────────────────────────────
    beta: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="Beta (5-year monthly, vs benchmark)",
    )

    # ── 52-week range ───────────────────────────────────────────────
    high_52w: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="52-week high price",
    )
    low_52w: Mapped[Decimal | None] = mapped_column(
        nullable=True,
        comment="52-week low price",
    )

    # ── Metadata ────────────────────────────────────────────────────
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Data source identifier (e.g. 'openbb', 'manual')",
    )
    provider_metadata: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Provider-specific additional metadata",
    )

    def __repr__(self) -> str:
        return (
            f"<FundamentalObservation security_id={self.security_id!r} "
            f"ts={self.timestamp.isoformat()!r}>"
        )
