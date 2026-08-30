"""Tenant-scoped, destination-neutral spending rules."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SpendingRule(TimestampMixin, Base):
    """A user-owned rule applied before destination-specific projection."""

    __tablename__ = "spending_rules"
    __table_args__: ClassVar = {
        "comment": "Tenant-scoped spending classification and enrichment rules"
    }

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict
    )
    actions: Mapped[dict[str, Any]] = mapped_column(
        _JSON, nullable=False, default=dict
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, default="user"
    )
