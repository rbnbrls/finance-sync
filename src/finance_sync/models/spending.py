"""Provider-neutral spending entities and provenance records."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class MerchantIdentity(TimestampMixin, Base):
    __tablename__ = "merchant_identities"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id", "stable_key", name="uq_merchant_identity_key"
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    stable_key: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mccs: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    normalization_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )


class TransactionSourceReference(TimestampMixin, Base):
    __tablename__ = "transaction_source_references"
    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ids: Mapped[list[str]] = mapped_column(
        _JSON, nullable=False, default=list
    )
    provider_revisions: Mapped[list[str] | None] = mapped_column(
        _JSON, nullable=True
    )
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON, nullable=True
    )


class TransactionSplit(TimestampMixin, Base):
    __tablename__ = "transaction_splits"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "transaction_id",
            "idempotency_key",
            name="uq_transaction_split_idempotency",
        ),
    )
    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    percentage: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 5), nullable=True
    )
    category_suggestion: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON, nullable=True
    )
    destination: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(64), nullable=False, default="user"
    )


class TransactionAnnotation(TimestampMixin, Base):
    __tablename__ = "transaction_annotations"
    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    annotation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_reference: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    destination_reference: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
