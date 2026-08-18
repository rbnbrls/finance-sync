"""Review queue for ambiguous security-identity resolutions.

When a market-intelligence item references a security whose identifiers
match multiple candidates (e.g. ``NOK`` or ``Apple`` without enough
context), the item is **never** silently attached to a holding.  Instead
one review-queue entry per item records the candidate list so a human
(or a later, richer pass) can resolve it.

Idempotency: the queue is unique on ``(tenant_id, item_id)`` — re-ingesting
the same item after an entry exists does not create a second entry and
never overwrites a previously accepted resolution.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class MarketIntelligenceReviewQueue(TimestampMixin, Base):
    """One review-queue entry for an ambiguously-resolved item."""

    __tablename__ = "market_intelligence_review_queue"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "item_id",
            name="uq_market_intel_review_item",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The ambiguous item this entry belongs to",
    )

    provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Provider key of the item",
    )
    source_id: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Provider-scoped item id (dedupe key)",
    )

    #: Candidate securities that matched the item's identifiers.
    candidate_identifiers: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Candidate security matches (id + identifier + confidence)",
    )
    resolution_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="pending/resolved/dismissed",
    )
    resolved_security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"),
        nullable=True,
        comment="Security chosen when the entry is resolved",
    )
    review_note: Mapped[str | None] = mapped_column(
        nullable=True,
        comment="Free-form note from the reviewer",
    )

    def __repr__(self) -> str:
        return (
            f"<MarketIntelligenceReviewQueue id={self.id!r} "
            f"item_id={self.item_id!r} status={self.resolution_status!r}>"
        )


#: Allowed review-queue resolution statuses.
INTEL_REVIEW_STATUSES = frozenset({"pending", "resolved", "dismissed"})
