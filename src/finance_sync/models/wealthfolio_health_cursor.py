"""Tenant-scoped state for authenticated Wealthfolio health polling."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class WealthfolioHealthCursor(Base):
    """Last safe observation for one tenant and Wealthfolio export target.

    Only aggregate state is persisted.  The health response itself can contain
    sensitive asset details and is intentionally never stored here.
    """

    __tablename__ = "wealthfolio_health_cursors"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "target_id",
            name="uq_wealthfolio_health_cursor_tenant_target",
        ),
        Index(
            "ix_wealthfolio_health_cursor_tenant_success",
            "tenant_id",
            "last_successful_poll",
        ),
        Index(
            "ix_wealthfolio_health_cursor_target",
            "target_id",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("export_targets.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_successful_poll: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()
