"""Persistent, optional downstream destinations for the personal datalake.

The canonical finance-sync database is the source of truth.  An
``ExportTarget`` is only a consumer configuration: removing or pausing it can
never remove canonical accounts, transactions, holdings or securities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts

TARGET_WEALTHFOLIO = "wealthfolio"
TARGET_ACTUAL_BUDGET = "actual-budget"
TARGET_JUPYTER = "jupyter"
TARGET_FIREFLY = "firefly"
TARGET_GHOSTFOLIO = "ghostfolio"
TARGET_INVESTBRAIN = "investbrain"
TARGET_SECURO = "securo"
TARGET_TYPES = {
    TARGET_WEALTHFOLIO,
    TARGET_ACTUAL_BUDGET,
    TARGET_JUPYTER,
    TARGET_FIREFLY,
    TARGET_GHOSTFOLIO,
    TARGET_INVESTBRAIN,
    TARGET_SECURO,
}

TARGET_DRAFT = "draft"
TARGET_ACTIVE = "active"
TARGET_PAUSED = "paused"
TARGET_STATUSES = {TARGET_DRAFT, TARGET_ACTIVE, TARGET_PAUSED}


class ExportTarget(Base):
    """A tenant-scoped optional consumer destination.

    ``configuration`` deliberately contains only non-secret settings (URL,
    account mapping preferences, etc.).  The JSON secret payload is encrypted
    with the existing envelope-encryption service before it reaches the
    database, and is never serialised by the API.
    """

    __tablename__ = "export_targets"
    __table_args__: ClassVar = {
        "comment": (
            "Optional downstream consumers; canonical data remains local"
        ),
    }

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TARGET_DRAFT,
        server_default=TARGET_DRAFT,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    selected_account_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    datasets: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    encrypted_secret: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    secret_nonce: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    jupyter_api_key_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    schedule_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_schedules.id", ondelete="SET NULL"), nullable=True
    )
    last_health_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    last_health_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at = created_at_ts()
    updated_at = updated_at_ts()
