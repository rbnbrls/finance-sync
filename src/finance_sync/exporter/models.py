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

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class ExportRun(Base):
    """Tracks a single export run.

    Each run stores its outcome so downstream alerting / dashboards
    can observe export health.
    """

    __tablename__ = "export_runs"

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── State ────────────────────────────────────────────────────────
    exporter_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "Exporter key ('wealthfolio', 'actual-budget', 'firefly') "
            "that ran this"
        ),
    )
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
    error_category: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Stable operational category for a failed export",
    )
    target_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="Destination identifier; legacy exports use NULL",
    )
    account_scope: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Explicit account identifiers included in the export",
    )
    delivery_checkpoint: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Sanitized delivery cursor/checkpoint metadata",
    )
    preflight_manifest: Mapped[dict[str, object] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Versioned Wealthfolio data-contract validation result",
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ExportRun id={self.id!r} status={self.status!r} "
            f"exported={self.transactions_exported!r}>"
        )
