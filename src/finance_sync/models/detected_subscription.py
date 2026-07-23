"""Detected subscription model — recurring transaction pattern findings.

Each row represents a subscription identified by analyzing transaction
history for recurring amounts, regular intervals, and merchant patterns.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid
from finance_sync.models.enums import (
    DetectionMethod,
    SubscriptionConfidence,
    SubscriptionStatus,
)


class DetectedSubscription(Base):
    """A recurring transaction pattern identified as a subscription.

    Stores the merchant/description, amount, frequency, and confidence
    of a detected subscription derived from transaction history analysis.
    """

    __tablename__ = "detected_subscriptions"
    __table_args__: ClassVar = (
        {
            "comment": (
                "Recurring transaction patterns identified as subscriptions"
            )
        },
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── Merchant / description ──────────────────────────────────────
    merchant_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Normalised merchant or counterparty name",
    )
    raw_description: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Most recent raw transaction description",
    )

    # ── Financial pattern ───────────────────────────────────────────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        comment="Typical subscription amount (negative = outgoing payment)",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3),
        default="EUR",
        nullable=False,
        comment="ISO-4217 currency code",
    )
    frequency_days: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Detected interval in calendar days between charges",
    )
    frequency_label: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Human-readable frequency: monthly, weekly, yearly, etc.",
    )

    # ── Confidence / classification ────────────────────────────────
    confidence: Mapped[SubscriptionConfidence] = mapped_column(
        String(16),
        default=SubscriptionConfidence.MEDIUM,
        nullable=False,
        comment="'high', 'medium', or 'low'",
    )
    detection_method: Mapped[DetectionMethod] = mapped_column(
        String(32),
        default=DetectionMethod.EXACT_AMOUNT,
        nullable=False,
        comment="How the subscription was detected",
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        String(16),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
        comment="'active', 'paused', 'cancelled', 'ignored', 'unknown'",
    )

    # ── Transaction references ──────────────────────────────────────
    transaction_ids: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="IDs of the matched transactions",
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Primary account for this subscription",
    )
    provider_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Primary connector provider"
    )

    # ── Merchant classification ─────────────────────────────────────
    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"),
        nullable=True,
        comment="Linked security (if merchant is a listed company)",
    )
    sector: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Merchant sector classification from fundamentals data",
    )
    category: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Subscription category: streaming, software, utilities, etc.",
    )

    # ── Temporal ────────────────────────────────────────────────────
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Earliest matched transaction date",
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Most recent matched transaction date",
    )
    occurrence_count: Mapped[int] = mapped_column(
        default=1, nullable=False, comment="Number of matched occurrences"
    )

    # ── Metadata ────────────────────────────────────────────────────
    detection_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="Algorithmic confidence score (0.0–1.0)",
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Extra detection context (intervals, amount variance, etc.)",
    )
    user_notes: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="User-provided notes or label override",
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<DetectedSubscription id={self.id!r} "
            f"merchant={self.merchant_name!r} "
            f"amount={self.amount!r} "
            f"confidence={self.confidence!r}>"
        )
