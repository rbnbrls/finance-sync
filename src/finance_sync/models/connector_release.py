"""Versioned connector release records used by the promotion gate."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import JSON, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class ConnectorRelease(Base):
    """One immutable-ish release state per provider/version."""

    __tablename__ = "connector_releases"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "provider_key", "version", name="uq_connector_release_version"
        ),
    )

    id: Mapped[str] = pk_uuid()
    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="candidate"
    )
    previous_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    certification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    certification_commit: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    compatibility_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    canary_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    capabilities: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at = created_at_ts()
