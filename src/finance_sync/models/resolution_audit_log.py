"""Audit trail for all security identity resolution decisions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class ResolutionAuditLog(TimestampMixin, Base):
    """Immutable audit record of every resolution decision.

    Records who/what decided that an incoming security identity maps
    to a canonical Security, and how that decision was reached.
    """

    __tablename__ = "resolution_audit_log"

    id: Mapped[str] = pk_uuid()

    # ── Context ─────────────────────────────────────────────────────────
    unresolved_security_id: Mapped[str | None] = mapped_column(
        ForeignKey("unresolved_securities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="The unresolved record (if any) that triggered this",
    )
    source_security_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="The incoming security identifier (ISIN, ticker, etc.)",
    )
    target_security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Canonical Security the source was mapped to",
    )

    # ── Decision ────────────────────────────────────────────────────────
    resolution_method: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="auto_isin / auto_figi / auto_ticker / fuzzy_name / manual",
    )
    confidence: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="high",
        comment="exact / high / medium / low",
    )
    resolver_principal: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="system",
        comment="Who/what performed the resolution: 'system' or user ID",
    )
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the resolution decision was made",
    )

    # ── Details ─────────────────────────────────────────────────────────
    resolution_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable explanation of how the decision was reached",
    )
    match_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="Match confidence score (0.0-1.0) for fuzzy matches",
    )

    def __repr__(self) -> str:
        return (
            f"<ResolutionAuditLog target={self.target_security_id!r} "
            f"method={self.resolution_method!r} confidence={self.confidence!r}>"
        )
