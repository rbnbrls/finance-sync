"""Durable data-quality remediation backlog."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID as _UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class DataQualityRemediationItem(Base):
    """One deduplicated, at-least-once remediation task.

    Provider credentials and raw provider responses must never be placed in
    ``context``.  The executor is the only component allowed to load them.
    """

    __tablename__ = "data_quality_remediation_items"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "deduplication_key",
            name="uq_dq_remediation_tenant_dedup",
        ),
        Index(
            "ix_dq_remediation_due",
            "status",
            "next_attempt_at",
            "priority",
            "first_detected_at",
        ),
        Index(
            "ix_dq_remediation_tenant_status",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_dq_remediation_provider_status",
            "provider_key",
            "status",
            "next_attempt_at",
        ),
        Index("ix_dq_remediation_lease", "lease_expires_at"),
        Index(
            "ix_dq_remediation_batch",
            "tenant_id",
            "batch_key",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("export_targets.id", ondelete="SET NULL"),
        nullable=True,
    )
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    affected_entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    rate_limit_deferral_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    remediation_strategy: Mapped[str] = mapped_column(
        String(96), nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    claim_token: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
