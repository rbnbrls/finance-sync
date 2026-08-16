"""Auditable DEGIRO file-import attempt."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class ImportRun(Base):
    """Tracks previews and confirmed upload/watchfolder imports."""

    __tablename__ = "import_runs"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "batch_hash",
            "attempt",
            name="uq_import_run_batch_attempt",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    batch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(default=1, nullable=False)
    report_types: Mapped[list[str]] = mapped_column(JSONB, default=list)
    content_hashes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    file_names: Mapped[list[str]] = mapped_column(JSONB, default=list)
    storage_names: Mapped[list[str]] = mapped_column(JSONB, default=list)
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_total: Mapped[int] = mapped_column(default=0, nullable=False)
    created_count: Mapped[int] = mapped_column(default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(default=0, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)
    error_details: Mapped[list[str]] = mapped_column(JSONB, default=list)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    audit_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list
    )
    retained: Mapped[bool] = mapped_column(default=False, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()

    @property
    def safe_error(self) -> str | None:
        details = self.error_details or []
        return details[0] if details else None
