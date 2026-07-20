"""Provider-agnostic financial account model."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import AccountType
from finance_sync.models.mixins import TimestampMixin


class Account(TimestampMixin, Base):
    """A provider-agnostic financial account (cash, bank, brokerage, …).

    ``provider_metadata`` holds provider-specific attributes in JSONB so
    the schema stays open across connectors without migration churn.
    """

    __tablename__ = "accounts"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_account_id",
            name="uq_accounts_provider",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── Provider identity ────────────────────────────────────────────
    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="e.g. 'plaid', 'teller', 'openbb'"
    )
    external_account_id: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="Provider's account ID"
    )

    # ── Display ──────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="Human-readable account name"
    )
    account_type: Mapped[AccountType] = mapped_column(
        String(64),
        nullable=False,
        comment="checking/savings/brokerage/credit/loan/investment",
    )
    account_subtype: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), default="EUR", nullable=False, comment="ISO-4217"
    )

    # ── Balances ─────────────────────────────────────────────────────
    current_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    available_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    iso_currency_code: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217 for current balance"
    )

    # ── Provider metadata ────────────────────────────────────────────
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # ── Lifecycle ────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<Account id={self.id!r} name={self.name!r} "
            f"type={self.account_type!r}>"
        )
