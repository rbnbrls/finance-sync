"""Append-only run registry for market-intelligence provider refreshes.

One row per scheduler run of a (tenant, provider) pair — the **history**
behind the single latest-run row in
:class:`MarketIntelligenceProviderState`.  Runs are observable: every
run records started/completed timestamps, duration, quota usage,
freshness snapshot, capability availability and a **sanitised** error
message (secrets redacted before persistence).

The table is append-only by design (never updated in place): the state
table carries the *latest* outcome for cadence decisions, this table
carries the *trail* for observability and post-mortems.  A provider
outage therefore never removes previously valid observations and always
leaves an explicit run trail.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class MarketIntelligenceRun(TimestampMixin, Base):
    """One recorded scheduler run of a market-intelligence provider."""

    __tablename__ = "market_intelligence_runs"
    __table_args__: ClassVar = (
        Index(
            "ix_market_intel_runs_tenant_provider_started",
            "tenant_id",
            "provider",
            "started_at",
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
        comment="Provider key, e.g. 'openbb', 'sec', 'sec_press'",
    )

    # ── Run window ─────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the scheduler run started",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the run completed (null while still running)",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="pending/ok/degraded/unavailable",
    )
    forced: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        comment="True when the run was forced (ignored cadence)",
    )

    # ── Metrics ────────────────────────────────────────────────────
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Run duration in milliseconds",
    )
    items_ingested: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Items ingested during the run",
    )
    quota_used: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Quota used by the run (provider-specific)",
    )
    quota_limit: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Quota limit of the provider window",
    )

    # ── Outcome ────────────────────────────────────────────────────
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Sanitised error message (secrets redacted)",
    )
    error_class: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Exception class name of the failure",
    )

    # ── Freshness snapshot ─────────────────────────────────────────
    freshness_max_age_seconds: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Provider freshness max-age at run time (seconds)",
    )
    freshness_min_interval_seconds: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Provider min re-fetch interval at run time (seconds)",
    )
    capabilities: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Capabilities advertised by the provider at run time",
    )
    availability: Mapped[dict[str, str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Capability → availability mapping at run time",
    )

    def __repr__(self) -> str:
        return (
            f"<MarketIntelligenceRun tenant_id={self.tenant_id!r} "
            f"provider={self.provider!r} status={self.status!r} "
            f"started_at={self.started_at!r}>"
        )


#: Allowed run-registry status values (mirrors provider state).
INTEL_RUN_STATUSES = {"pending", "ok", "degraded", "unavailable"}
