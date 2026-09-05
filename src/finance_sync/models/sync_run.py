"""Ingestion run tracking model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import JSON, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    __table_args__ = (
        Index(
            "uq_sync_runs_active_connection",
            "connection_id",
            unique=True,
            postgresql_where=text(
                "status = 'running' AND connection_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[str] = pk_uuid()

    # ── Identity ─────────────────────────────────────────────────────
    connector: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connector name, e.g. 'plaid', 'teller', 'openbb'",
    )
    connection_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Stable connection (credential) id this run belongs to, "
            "when the sync was performed for a specific connection"
        ),
    )
    resource: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    # ── Watermark ────────────────────────────────────────────────────
    # Set to the run's start timestamp when the run completes
    # successfully — the same value the orchestrator persists to the
    # ``sync_cursor`` table per resource.  NULL on failure: a failed
    # run never advances the incremental sync position.
    cursor: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Watermark this run advanced the sync cursor to (set on "
            "successful completion); NULL on failure"
        ),
    )

    # ── Outcome ──────────────────────────────────────────────────────
    items_processed: Mapped[int | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Stable operational category for a failed run",
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    retry_after_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit_attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    rate_limit_scope: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    last_http_status: Mapped[int | None] = mapped_column(nullable=True)
    report: Mapped[dict[str, int] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Counts for new/changed/unchanged/classified/skipped/failed",
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<SyncRun id={self.id!r} connector={self.connector!r} "
            f"status={self.status!r}>"
        )
