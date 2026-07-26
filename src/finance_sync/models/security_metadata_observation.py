"""Structured metadata observations for securities.

Stores point-in-time observations of structured metadata such as
ETF composition (holdings + weights), sector/industry exposure,
and region allocations.  The actual payload is stored in a flexible
JSONB column keyed by metadata_type.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin


class SecurityMetadataObservation(TimestampMixin, Base):
    """A point-in-time observation of structured security metadata.

    Supports multiple metadata types via the ``metadata_type`` discriminator:
    - ``etf_composition`` — holdings, weights, sector/region allocation
    - ``sector_exposure`` — GICS sector/industry classification
    - ``fundamental_ratios`` — high-level fundamental ratio snapshot
    - ``company_profile`` — headquarters, description, employee count

    Deduplicated by (security_id, metadata_type, timestamp, source).
    """

    __tablename__ = "security_metadata_observations"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "security_id",
            "metadata_type",
            "timestamp",
            "source",
            name="uq_sec_metadata_obs_type_ts_source",
        ),
    )

    id: Mapped[str] = pk_uuid()

    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    metadata_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Discriminator: etf_composition, sector_exposure, "
        "fundamental_ratios, company_profile",
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="When the metadata observation was recorded",
    )

    # ── Payload ─────────────────────────────────────────────────────
    metadata_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Arbitrary structured metadata payload",
    )

    label: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Human-readable label for this observation "
        "(e.g. ETF name, sector title)",
    )

    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Data source identifier (e.g. 'openbb', 'manual')",
    )

    def __repr__(self) -> str:
        return (
            f"<SecurityMetadataObservation "
            f"security_id={self.security_id!r} "
            f"type={self.metadata_type!r}>"
        )
