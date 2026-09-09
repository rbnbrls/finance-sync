"""Canonical normalized transaction model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID as _UUID

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import TransactionStatus, TransactionType
from finance_sync.models.mixins import TimestampMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class Transaction(TimestampMixin, Base):
    """A canonical financial transaction."""

    __tablename__ = "transactions"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "connection_id",
            "external_transaction_id",
            name="uq_transactions_provider",
            # NULL connection_ids must still deduplicate (re-sync without
            # a connection scope would otherwise insert duplicates because
            # plain UNIQUE treats NULLs as distinct).
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Ingestion connector name"
    )
    connection_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Stable connection (credential) id this transaction was "
            "fetched with; scopes the external transaction id so two "
            "connections never collide"
        ),
    )
    external_transaction_id: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="Provider's transaction ID"
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    security_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        comment="Signed amount (positive = inflow, negative = outflow)",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="ISO-4217"
    )
    amount_in_base: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="Amount in tenant base currency"
    )
    base_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217 for amount_in_base"
    )
    fx_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True, comment="FX rate used for conversion"
    )

    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Number of units / shares transacted (for purchase/sale)",
    )
    unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Provider-reported unit price in instrument currency",
    )
    fee_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Provider-reported fee as a positive amount",
    )
    fee_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217 for fee_amount"
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the transaction actually occurred (provider time)",
    )
    booked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the provider booked / settled the transaction",
    )

    transaction_type: Mapped[TransactionType] = mapped_column(
        String(64),
        nullable=False,
        comment="transfer/payment/purchase/sale/fee/interest/dividend/"
        "tax/withdrawal/deposit/other",
    )
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        String(32),
        default=TransactionStatus.PENDING,
        nullable=False,
        comment="'pending', 'booked', 'reversed', 'cancelled'",
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    provider_fingerprint: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Provider-side checksum / hash"
    )
    provider_metadata_contract: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON,
        nullable=True,
        comment="Versioned, privacy-filtered provider fields",
    )
    merchant_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    merchant_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    merchant_city: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    merchant_country: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    counterparty_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    counterparty_account_reference: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    merchant_category_code: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    original_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    authorization_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    settlement_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    source_record_hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    cashflow_bucket: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    cashflow_suggestion: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON, nullable=True
    )
    classification_source: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    classification_override: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    gross_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    gross_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    net_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    net_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    tax_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    tax_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    refund_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    refund_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    revision: Mapped[int] = mapped_column(default=1, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id!r} amount={self.amount!r} "
            f"type={self.transaction_type!r} status={self.status!r}>"
        )
