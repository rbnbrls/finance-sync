"""Webhook registration and delivery tracking models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import (
    Base,
    created_at_ts,
    pk_uuid,
    tenant_fk,
    updated_at_ts,
)
from finance_sync.models.enums import WebhookDeliveryStatus


class Webhook(Base):
    """A registered webhook endpoint for event notifications."""

    __tablename__ = "webhooks"

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = tenant_fk()

    url: Mapped[str] = mapped_column(
        String(2048), nullable=False, comment="Webhook callback URL"
    )
    secret: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="HMAC-SHA256 signing secret"
    )
    events: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of event types this webhook subscribes to, e.g. ['sync.completed']",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Optional human-readable label"
    )

    # Rate-limit tracking
    rate_limit_max_per_minute: Mapped[int] = mapped_column(
        Integer,
        default=60,
        nullable=False,
        comment="Max deliveries allowed per 60-second sliding window",
    )

    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<Webhook id={self.id!r} url={self.url!r} "
            f"events={self.events!r} active={self.is_active}>"
        )


class WebhookDeliveryLog(Base):
    """Record of a single webhook delivery attempt."""

    __tablename__ = "webhook_delivery_logs"

    id: Mapped[str] = pk_uuid()
    webhook_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
        comment="FK to webhooks.id (no actual FK constraint for audit safety)",
    )
    tenant_id: Mapped[str] = tenant_fk()

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="e.g. 'sync.completed'"
    )
    event_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Source event / outbox-message id for tracing",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="The full JSON payload sent"
    )

    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        String(20),
        default=WebhookDeliveryStatus.PENDING,
        nullable=False,
        comment="'pending', 'delivered', 'failed', 'rate_limited'",
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When to retry next (null if max attempts reached or delivered)",
    )

    response_status_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    response_body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Truncated response body",
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Round-trip duration in milliseconds",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<WebhookDeliveryLog id={self.id!r} "
            f"webhook={self.webhook_id!r} event={self.event_type!r} "
            f"status={self.status!r} attempt={self.attempt_number}>"
        )
