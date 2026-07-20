"""Ingestion run tracking model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid
from finance_sync.models.enums import SyncRunStatus


class SyncRun(Base):
    """Tracks a single ingestion / sync run.

    Each connector creates a new ``SyncRun`` when it starts, updates the
    status as it progresses, and records the final state on completion.
    This provides observability and a basis for alerting on stuck/failed
    runs.
    """

    __tablename__ = "sync_runs"

    id: Mapped[str] = pk_uuid()

    # ── Identity ─────────────────────────────────────────────────────
    connector: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connector name, e.g. 'plaid', 'teller', 'openbb'",
    )

    # ── State ────────────────────────────────────────────────────────
    status: Mapped[SyncRunStatus] = mapped_column(
        String(16),
        default=SyncRunStatus.RUNNING,
        nullable=False,
        comment="'running', 'completed', 'failed', 'cancelled'",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Outcome ──────────────────────────────────────────────────────
    items_processed: Mapped[int | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<SyncRun id={self.id!r} connector={self.connector!r} "
            f"status={self.status!r}>"
        )
