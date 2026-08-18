"""Market-intelligence provider run-state persistence.

One row per (tenant, provider) records the latest scheduler run:
timestamps, latency, quota usage, freshness and a sanitised error
message (secrets redacted).  A provider outage therefore never removes
previously valid data and always leaves an explicit trail.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class MarketIntelligenceProviderState(TimestampMixin, Base):
    """Run/freshness state of one market-intelligence provider."""

    __tablename__ = "market_intelligence_provider_states"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            name="uq_market_intel_provider_state",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Provider key, e.g. 'openbb', 'sec'",
    )

    # ── Run tracking ────────────────────────────────────────────────
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last scheduler run started",
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last successful run completed",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Sanitised error of the last failed run (secrets redacted)",
    )
    last_error_class: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Exception class name of the last failure",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="pending/ok/degraded/unavailable",
    )

    # ── Metrics ─────────────────────────────────────────────────────
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Last run latency in milliseconds",
    )
    items_ingested: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Items ingested in the last successful run",
    )
    quota_used: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Quota used since last reset (provider-specific)",
    )
    quota_limit: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Quota limit of the provider window",
    )
    freshness_max_age_seconds: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Configured freshness max-age in seconds",
    )
    freshness_min_interval_seconds: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Configured minimum re-fetch interval in seconds",
    )
    capabilities: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Capabilities advertised by the provider at last run",
    )
    availability: Mapped[dict[str, str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Capability → availability mapping at last run",
    )

    def __repr__(self) -> str:
        return (
            f"<MarketIntelligenceProviderState tenant_id={self.tenant_id!r} "
            f"provider={self.provider!r} status={self.status!r}>"
        )


#: Allowed provider-state status values.
INTEL_PROVIDER_STATUSES = {"pending", "ok", "degraded", "unavailable"}
