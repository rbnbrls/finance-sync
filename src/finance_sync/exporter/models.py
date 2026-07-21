"""ORM models for exporters.

ExportRun
    Tracks each export attempt (analogous to SyncRun for ingestion).
    Shared by all exporters — Wealthfolio, Actual Budget, etc.

The ActualBudgetAccountMapping and ExportDelivery models now live in
``finance_sync.exporter.actual_budget.models``.
This module re-exports ExportRun for backward compatibility.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class ExportRun(Base):
    """Tracks a single export run.

    Each run stores its outcome so downstream alerting / dashboards
    can observe export health.
    """

    __tablename__ = "export_runs"

    id: Mapped[str] = pk_uuid()

    # ── State ────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        default="running",
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
    transactions_attempted: Mapped[int | None] = mapped_column(nullable=True)
    transactions_exported: Mapped[int | None] = mapped_column(nullable=True)
    transactions_failed: Mapped[int | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ExportRun id={self.id!r} status={self.status!r} "
            f"exported={self.transactions_exported!r}>"
        )
