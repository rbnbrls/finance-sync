"""API key model for machine-client authentication."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class ApiKey(Base):
    """A tenant-scoped API key for machine-to-machine auth."""

    __tablename__ = "api_keys"

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(
        String(8), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    permissions: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Space-separated permission strings, e.g. "
        "'transactions:read transactions:write'",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at = created_at_ts()

    __table_args__: ClassVar = (
        {"comment": "Persistent API keys for machine clients"},
    )
