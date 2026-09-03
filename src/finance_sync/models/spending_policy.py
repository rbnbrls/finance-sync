"""Tenant-scoped privacy and retention policy for spending metadata."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SpendingPrivacyPolicy(TimestampMixin, Base):
    __tablename__ = "spending_privacy_policies"
    __table_args__: ClassVar = (
        UniqueConstraint("tenant_id", name="uq_spending_privacy_policy_tenant"),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    enabled_fields: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list
    )
    retention_days: Mapped[int] = mapped_column(
        nullable=False, default=365, server_default="365"
    )
    allow_attachments: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    allow_raw_payload: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, default="user"
    )
