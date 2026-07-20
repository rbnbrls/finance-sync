"""Raw incoming security identity that could not be auto-resolved.

Records all identifiers supplied by a connector for a security that
failed automatic resolution.  These records populate the manual-review
queue so a human (or future automation) can map them to canonical
Security records.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class UnresolvedSecurity(TimestampMixin, Base):
    """A security from a connector that could not be auto-resolved.

    Stores the raw identifiers as received from the connector so that
    a human operator (or future fuzzy-matching run) can decide the
    canonical mapping.

    Once resolved (via the manual API or a later automated pipeline
    pass), ``resolved_security_id`` is set and the row stays as an
    audit trail.
    """

    __tablename__ = "unresolved_securities"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "provider_key",
            "external_security_id",
            name="uq_unresolved_provider_ext_id",
        ),
    )

    id: Mapped[str] = pk_uuid()

    # ── Connector provenance ────────────────────────────────────────────
    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="Connector name"
    )
    external_security_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Provider-local security / instrument ID",
    )

    # ── Raw identifiers as received ─────────────────────────────────────
    raw_isin: Mapped[str | None] = mapped_column(
        String(12), nullable=True, comment="ISIN as provided by connector"
    )
    raw_figi: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="FIGI / FIGI-like code as provided"
    )
    raw_ticker: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Ticker / symbol as provided"
    )
    raw_name: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Instrument name / description"
    )
    raw_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="Currency code as provided"
    )
    raw_metadata: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded provider-specific metadata",
    )

    # ── Resolution state ────────────────────────────────────────────────
    resolved_security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Canonical Security this was mapped to "
        "(null = still unresolved)",
    )
    resolution_method: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="How it was resolved: auto_isin / auto_figi / auto_ticker / "
        "fuzzy_name / manual",
    )
    resolution_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Human notes from manual resolution"
    )

    @property
    def is_resolved(self) -> bool:
        """Whether this record has been mapped to a canonical security."""
        return self.resolved_security_id is not None

    def __repr__(self) -> str:
        status = "resolved" if self.is_resolved else "unresolved"
        return (
            f"<UnresolvedSecurity id={self.id!r} "
            f"provider={self.provider_key!r} "
            f"ext_id={self.external_security_id!r} status={status}>"
        )
