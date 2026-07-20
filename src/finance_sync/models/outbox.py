"""Transactional outbox model for reliable event publishing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid
from finance_sync.models.enums import OutboxMessageStatus


class OutboxMessage(Base):
    """A message in the transactional outbox."""

    __tablename__ = "outbox_messages"

    id: Mapped[str] = pk_uuid()

    aggregate_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Aggregate root ID that produced this event",
    )
    aggregate_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="e.g. 'account', 'transaction', 'connection'",
    )
    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'account.created', 'transaction.booked'",
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
        comment="Optional idempotency key for exactly-once delivery",
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=False, comment="Serialised event data"
    )

    status: Mapped[OutboxMessageStatus] = mapped_column(
        String(16),
        default=OutboxMessageStatus.PENDING,
        nullable=False,
        comment="'pending', 'sent', 'failed'",
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<OutboxMessage id={self.id!r} "
            f"event={self.event_type!r} status={self.status!r}>"
        )
