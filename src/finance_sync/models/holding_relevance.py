"""Holding-relevance feed models.

Links market-intelligence observations to the tenant's current (or
recently sold) holdings, clusters syndicated stories, tracks per-user
acknowledgements and false-positive corrections, and records opt-in
notification state.

Design rules (see backlog/plus-relevant-nieuws-en-events.md):

* **Deterministic facts only.**  The security match, holding status,
  dates and source references are computed by finance-sync, never by an
  LLM.  Hermes may later *explain* relevance in a few sentences, but it
  can only cite these rows.
* **Tenant + user scoped.**  Every row carries ``tenant_id``; acks and
  corrections are additionally scoped to a ``user_id`` so one
  household's feedback never affects another.
* **Soft corrections.**  A correction never deletes the underlying
  observation; it suppresses the match for the correcting user's feed
  and feeds the future matcher.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

#: Match reasons recorded on a relevance item.
MATCH_REASON_EXACT_SECURITY = "canonical_security"
MATCH_REASON_RECENTLY_SOLD = "recently_sold"
MATCH_REASON_CURRENCY_INTEREST = "currency_interest"
MATCH_REASON_HERMES_SUGGESTED = "hermes_suggested"

#: Allowed holding-status values.
HOLDING_STATUS_CURRENT = "current"
HOLDING_STATUS_RECENTLY_SOLD = "recently_sold"

#: Cluster event types (extensible; drives the calendar).
EVENT_TYPE_EARNINGS = "earnings"
EVENT_TYPE_DIVIDEND = "dividend"
EVENT_TYPE_AGM = "agm"
EVENT_TYPE_SPLIT = "split"
EVENT_TYPE_MERGER = "merger"
EVENT_TYPE_ACQUISITION = "acquisition"
EVENT_TYPE_FILING = "filing"
EVENT_TYPE_NEWS = "news"
EVENT_TYPE_INTEREST = "interest"
EVENT_TYPE_CURRENCY = "currency"

#: Freshness values served to clients.
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"

#: Correction actions.
CORRECTION_DISMISS = "dismiss"
CORRECTION_REASSIGN = "reassign"


class HoldingRelevanceItem(TimestampMixin, Base):
    """One intel observation matched to one security held by a tenant.

    The unique constraint is on ``(tenant_id, item_id, security_id)`` so
    a single syndicated item can never produce two relevance rows for
    the same holding, and a story that mentions several holdings gets
    one row per holding.
    """

    __tablename__ = "holding_relevance_items"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "item_id",
            "security_id",
            name="uq_holding_relevance_item_security",
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
        comment=(
            "The market-intelligence observation this relevance row "
            "derives from"
        ),
    )
    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "Canonical security that matched the item (NULL for "
            "cash/currency interest events without a security)"
        ),
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "Account holding the security (NULL for "
            "recently-sold/portfolio-wide)"
        ),
    )

    # ── Match provenance ────────────────────────────────────────────
    match_reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="canonical_security/recently_sold/currency_interest/hermes_suggested",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        comment="0..1 match confidence (1.0 for canonical security matches)",
    )
    holding_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=HOLDING_STATUS_CURRENT,
        comment="current/recently_sold",
    )
    holding_weight: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment=(
            "Normalised 0..1 position weight at match time (NULL for "
            "cash/unknown)"
        ),
    )

    #: Normalised event date used for clustering + calendar.  Derived
    #: from structured facts (ex/record/payment/meeting date, event
    #: date) or the item's published date for plain news.
    event_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Normalised event date (fact-driven, else published date)",
    )

    def __repr__(self) -> str:
        return (
            f"<HoldingRelevanceItem id={self.id!r} "
            f"security={self.security_id!r} reason={self.match_reason!r}>"
        )


class RelevanceCluster(TimestampMixin, Base):
    """One story: the deduplicated, ranked aggregation of matched items.

    ``story_key`` is the deterministic clustering identity:
    ``security_id + event_type + event_date`` (day granularity for
    plain news).  Distinct events (different quarter, ex-date vs
    payment date) always produce distinct clusters; syndicated coverage
    of the same event merges into one cluster that keeps every source
    link.
    """

    __tablename__ = "relevance_clusters"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "story_key",
            name="uq_relevance_cluster_story",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    story_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment=(
            "Deterministic clustering identity (security+event_type+event_date)"
        ),
    )

    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "Primary security of the story (NULL only for "
            "cash/currency stories)"
        ),
    )
    headline: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Best headline across the clustered items",
    )
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="earnings/dividend/agm/split/merger/acquisition/filing/news/interest/currency",
    )
    event_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Normalised event date of the story",
    )
    source_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of distinct source links in the cluster",
    )
    best_source_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Canonical URL of the most reliable source in the cluster",
    )
    score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment=(
            "Deterministic ranking score (weight x proximity x recency "
            "x reliability)"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<RelevanceCluster id={self.id!r} type={self.event_type!r} "
            f"date={self.event_date!r} score={self.score!r}>"
        )


class RelevanceClusterItem(TimestampMixin, Base):
    """Membership edge: one cluster keeps every source item + link."""

    __tablename__ = "relevance_cluster_items"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "cluster_id",
            "item_id",
            name="uq_relevance_cluster_item",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="One source item of the story",
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Deterministic ordering inside the cluster",
    )


class RelevanceAck(TimestampMixin, Base):
    """Per-user acknowledgement of one cluster (idempotent)."""

    __tablename__ = "relevance_acks"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "cluster_id",
            name="uq_relevance_ack_user_cluster",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="User id (plain string, survives user deletion)",
    )
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    acknowledged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the user acknowledged (NULL until first ack)",
    )


class RelevanceCorrection(TimestampMixin, Base):
    """Per-tenant/per-user false-positive correction.

    A correction *suppresses* the (item, security) match for the
    correcting user's feed and records the feedback so the future
    matcher can improve.  It never deletes the underlying
    market-intelligence observation and never affects other tenants.
    """

    __tablename__ = "relevance_corrections"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "item_id",
            name="uq_relevance_correction_user_item",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="User id that filed the correction",
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("market_intelligence_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The observation that was a false positive",
    )
    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Security the user corrected (NULL when dismissing generically)"
        ),
    )
    action: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CORRECTION_DISMISS,
        comment="dismiss/reassign",
    )
    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form user note (sanitised before persistence)",
    )


class RelevanceNotificationPreference(TimestampMixin, Base):
    """Opt-in notification settings per tenant + user.

    ``lockscreen_safe`` defaults to True: notification payloads never
    carry position sizes or financial values on the lockscreen.
    """

    __tablename__ = "relevance_notification_preferences"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_relevance_notif_pref_user",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Opt-in: notifications are off by default",
    )
    lockscreen_safe: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Never leak position size/financial value on the lockscreen",
    )
    event_types: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Allowed event types; NULL/empty = all",
    )


class RelevanceNotificationLog(TimestampMixin, Base):
    """Deduplication log: one row per (user, cluster, event type)."""

    __tablename__ = "relevance_notification_log"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "cluster_id",
            "event_type",
            name="uq_relevance_notif_log_dedupe",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("relevance_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="earnings/dividend/agm/split/merger/acquisition/filing/news/interest/currency",
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="When the notification payload was delivered",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Lockscreen-safe payload snapshot (no position sizes/values)",
    )
