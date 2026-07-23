"""Canonical normalized transaction model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import TransactionStatus, TransactionType
from finance_sync.models.mixins import TimestampMixin


class Transaction(TimestampMixin, Base):
    """A canonical financial transaction."""

    __tablename__ = "transactions"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_transaction_id",
            name="uq_transactions_provider",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Ingestion connector name"
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
        "withdrawal/deposit/other",
    )
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        String(32),
        default=TransactionStatus.PENDING,
        nullable=False,
        comment="'pending', 'booked', 'reversed', 'cancelled'",
    )

    provider_fingerprint: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Provider-side checksum / hash"
    )
    revision: Mapped[int] = mapped_column(default=1, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id!r} amount={self.amount!r} "
            f"type={self.transaction_type!r} status={self.status!r}>"
        )
