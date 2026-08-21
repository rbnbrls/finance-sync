"""Persisted refresh-token state for rotation and replay detection."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class RefreshToken(Base):
    """One issued refresh token, stored by hash and revoked on rotation."""

    __tablename__ = "refresh_tokens"
    __table_args__: ClassVar = (
        UniqueConstraint("jti", name="uq_refresh_tokens_jti"),
    )

    id: Mapped[str] = pk_uuid()
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by_jti: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_at = created_at_ts()
