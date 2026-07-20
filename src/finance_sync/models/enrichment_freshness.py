"""Tracks when each security was last enriched with market data."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Text as SA_Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class EnrichmentFreshness(TimestampMixin, Base):
    """Track the last time market data was fetched for a security."""

    __tablename__ = "enrichment_freshness"

    id: Mapped[str] = pk_uuid()

    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    last_metadata_fetch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When security metadata was last resolved",
    )
    last_quote_fetch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the latest quote was last fetched",
    )
    last_daily_price_fetch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When daily historical prices were last synced",
    )
    last_intraday_price_fetch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When intraday prices were last synced",
    )

    data_source: Mapped[str] = mapped_column(
        SA_Text,
        nullable=False,
        default="openbb",
        comment="Primary data source identifier",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="enrichment_pending/resolved/failed",
    )
    error_message: Mapped[str | None] = mapped_column(
        SA_Text,
        nullable=True,
        comment="Last error message if enrichment failed",
    )

    def __repr__(self) -> str:
        return (
            f"<EnrichmentFreshness security_id={self.security_id!r} "
            f"status={self.status!r}>"
        )
