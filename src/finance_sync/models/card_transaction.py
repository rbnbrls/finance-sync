"""Card transaction model.

Represents a debit / credit card payment transaction with merchant
details, card identification, and authorization lifecycle tracking.
Card transactions are distinct from bank transfers and include
additional fields such as MCC, merchant info, and authorization status.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import (
    CardAuthorizationType,
    TransactionStatus,
    TransactionType,
)
from finance_sync.models.mixins import TimestampMixin


class CardTransaction(TimestampMixin, Base):
    """A debit/credit card payment transaction.

    Carries merchant enrichment data (name, city, MCC) and tracks the
    full authorization lifecycle (authorization → settlement → refund).
    """

    __tablename__ = "card_transactions"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_card_transaction_id",
            name="uq_card_transactions_provider",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Ingestion connector name"
    )
    external_card_transaction_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Provider's card transaction identifier",
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
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

    # ── Merchant info ──────────────────────────────────────────────────
    merchant_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Merchant / store name"
    )
    merchant_city: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Merchant city"
    )
    merchant_country: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Merchant country"
    )
    mcc: Mapped[str | None] = mapped_column(
        String(4), nullable=True, comment="Merchant Category Code"
    )

    # ── Card info ──────────────────────────────────────────────────────
    card_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Provider card identifier"
    )
    card_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Card type: debit/credit/prepaid/virtual",
    )
    card_last_four: Mapped[str | None] = mapped_column(
        String(4), nullable=True, comment="Last four digits of card PAN"
    )

    # ── Timing & lifecycle ─────────────────────────────────────────────
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the transaction actually occurred (provider time)",
    )
    booked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the transaction settled / was booked",
    )

    transaction_type: Mapped[TransactionType] = mapped_column(
        String(32),
        default=TransactionType.CARD_PAYMENT,
        nullable=False,
        comment="card_payment / refund / fee / withdrawal / other",
    )
    authorization_type: Mapped[CardAuthorizationType] = mapped_column(
        String(32),
        default=CardAuthorizationType.AUTHORIZATION,
        nullable=False,
        comment="authorization / settlement / refund / chargeback / other",
    )
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        String(32),
        default=TransactionStatus.PENDING,
        nullable=False,
        comment="pending / booked / reversed / cancelled",
    )

    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    def __repr__(self) -> str:
        return (
            f"<CardTransaction id={self.id!r} amount={self.amount!r} "
            f"merchant={self.merchant_name!r} status={self.status!r}>"
        )
