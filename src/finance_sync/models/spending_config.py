"""Tenant-scoped merchant/category mappings and user overrides."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class MerchantMapping(TimestampMixin, Base):
    __tablename__ = "merchant_mappings"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id", "merchant_key", name="uq_merchant_mapping_key"
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    merchant_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str | None] = mapped_column(String(256), nullable=True)
    taxonomy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalization_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )


class CategoryMapping(TimestampMixin, Base):
    __tablename__ = "category_mappings"
    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    taxonomy: Mapped[str] = mapped_column(String(128), nullable=False)
    source_category: Mapped[str] = mapped_column(String(256), nullable=False)
    destination_type: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_category: Mapped[str] = mapped_column(
        String(256), nullable=False
    )


class TransactionOverride(TimestampMixin, Base):
    __tablename__ = "transaction_overrides"
    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, default="user"
    )


class DestinationObjectReference(TimestampMixin, Base):
    __tablename__ = "destination_object_references"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "destination_type",
            "idempotency_key",
            name="uq_destination_idempotency",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    destination_type: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    canonical_key: Mapped[str] = mapped_column(String(256), nullable=False)
    destination_object_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    direction: Mapped[str] = mapped_column(
        String(32), nullable=False, default="write"
    )
    source_revision: Mapped[int | None] = mapped_column(nullable=True)
