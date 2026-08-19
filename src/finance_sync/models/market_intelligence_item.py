"""Market-intelligence item persistence model.

Stores normalised observations from the provider-independent source
layer.  Every row keeps full provenance (provider, source id, canonical
URL), temporal metadata (published/fetched/validity), language,
licence class and a content hash so derived records stay traceable to
the original source and syndicated duplicates are deduplicated on
``(tenant_id, provider, source_id)`` plus ``content_hash``.

The storage policy is enforced by the ingestion service, not by this
model: full copyrighted text is never written here unless the source
licence class explicitly allows it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class MarketIntelligenceItem(TimestampMixin, Base):
    """A single stored market-intelligence observation."""

    __tablename__ = "market_intelligence_items"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "source_id",
            name="uq_market_intel_provider_source",
        ),
        UniqueConstraint(
            "tenant_id",
            "content_hash",
            name="uq_market_intel_content_hash",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Provenance ──────────────────────────────────────────────────
    provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Provider key, e.g. 'openbb', 'sec'",
    )
    source_id: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Provider-scoped stable item id (dedupe key)",
    )
    canonical_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Canonical URL / document id of the item",
    )

    kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Item kind: news_article/corporate_event/earnings_report/…",
    )

    # ── Time ────────────────────────────────────────────────────────
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="Publication time of the source item",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When finance-sync fetched the item",
    )
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Start of the item's validity window",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="End of the item's validity window",
    )

    # ── Language / licensing / integrity ────────────────────────────
    language: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="en",
        comment="BCP-47-ish language tag",
    )
    license_class: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="public_domain/open_license/free_access/subscriber_only/"
        "proprietary",
    )
    license_uri: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Link to the license terms, if any",
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="SHA-256 over the item's canonical identity",
    )

    # ── Freshness / staleness ───────────────────────────────────────
    stale_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Freshness deadline: when the provider's max_age elapses "
            "after fetched_at, the item may be marked stale.  NULL for "
            "items whose source declares no freshness bound."
        ),
    )
    is_stale: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
        comment=(
            "True when the item has aged past its provider's freshness "
            "bound.  Stale is a soft flag — the observation is never "
            "deleted or invalidated, it is only marked."
        ),
    )

    # ── Content ─────────────────────────────────────────────────────
    headline: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Headline/title (always storable)",
    )
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Short snippet (policy-gated by license class)",
    )
    body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Full text (only for permissive license classes)",
    )
    facts: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured facts derived from the item",
    )
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Provider-specific non-secret metadata",
    )

    # ── Security identity (resolution output) ───────────────────────
    identifiers: Mapped[dict[str, str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Candidate security identifiers (ticker/isin/figi/cik)",
    )
    resolution_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unresolved",
        comment="resolved/ambiguous/unresolved/ignored",
    )
    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Canonical security when resolved",
    )
    review_required: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="True when the item needs manual review (ambiguous match)",
    )

    def __repr__(self) -> str:
        return (
            f"<MarketIntelligenceItem id={self.id!r} "
            f"provider={self.provider!r} source_id={self.source_id!r}>"
        )
