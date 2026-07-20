"""Time-versioned account balance snapshot model."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import BalanceKind, BalanceSource
from finance_sync.models.mixins import TimestampMixin


class Balance(TimestampMixin, Base):
    """A point-in-time snapshot of an account balance."""

    __tablename__ = "balances"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "account_id",
            "observed_at",
            "balance_kind",
            name="uq_balances_snapshot",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When this balance was observed",
    )
    balance_kind: Mapped[BalanceKind] = mapped_column(
        String(32),
        nullable=False,
        comment="'available', 'booked', 'current', 'limit', 'cash'",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="Balance amount"
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="ISO-4217"
    )

    source: Mapped[BalanceSource] = mapped_column(
        String(64),
        nullable=False,
        comment="'provider_sync', 'manual_entry', 'computed'",
    )

    def __repr__(self) -> str:
        return (
            f"<Balance id={self.id!r} account={self.account_id!r} "
            f"kind={self.balance_kind!r} amount={self.amount!r}>"
        )
