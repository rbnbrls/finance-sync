"""Append-only lifecycle events for canonical transactions."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid

_JSON = JSON().with_variant(JSONB(), "postgresql")


class TransactionLifecycleEvent(Base):
    __tablename__ = "transaction_lifecycle_events"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "transaction_id",
            "idempotency_key",
            name="uq_transaction_event_idempotency",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict
    )
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, default="provider_sync"
    )
    source_revision: Mapped[int | None] = mapped_column(nullable=True)
    created_at = created_at_ts()
