"""Tax lot tracking model for cost basis and realized/unrealized P&L.

A tax lot represents a discrete block of shares acquired in a single
transaction. When shares are sold, the sale is matched against one or
more purchase lots to determine cost basis and realised P&L. Lots can
be partially or fully closed and may have wash sale adjustments.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import CostBasisMethod
from finance_sync.models.mixins import TimestampMixin


class TaxLot(TimestampMixin, Base):
    """A discrete block of shares acquired in a single transaction.

    Open lots have ``closed_at`` = ``None``. A lot is fully closed when its
    ``remaining_quantity`` reaches 0.  Partial sales split an existing lot
    into a closed portion (realised) and a new open lot (carry-over).
    """

    __tablename__ = "tax_lots"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "security_id",
            "purchase_transaction_id",
            "acquired_at",
            name="uq_tax_lots_purchase",
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
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ── Transaction links ─────────────────────────────────────────────
    purchase_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Transaction that created this lot",
    )
    sale_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Transaction that fully or partially closed this lot",
    )

    # ── Lot quantities ────────────────────────────────────────────────
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        comment="Original number of units acquired (positive)",
    )
    remaining_quantity: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        default=0,
        comment="Units still held in this lot (decreases on partial sales)",
    )

    # ── Cost basis ────────────────────────────────────────────────────
    cost_basis_total: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        comment="Total cost of this lot (local currency)",
    )
    cost_basis_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(24, 8),
        nullable=False,
        comment="Cost per unit = cost_basis_total / quantity",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="ISO-4217"
    )

    # ── Dates ─────────────────────────────────────────────────────────
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the lot was acquired (trade / settlement date)",
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the lot was fully closed (null if still open)",
    )

    # ── Realised P&L ──────────────────────────────────────────────────
    realized_pl: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Realised P&L when this lot was closed (proceeds - cost)",
    )
    realized_pl_currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="ISO-4217 for realised P&L"
    )

    # ── Wash sale fields ──────────────────────────────────────────────
    has_wash_sale_adjustment: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        comment="True if a wash sale adjustment was applied to this lot",
    )
    disallowed_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Loss disallowed due to wash sale rules",
    )
    wash_sale_adjustment_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="loss_disallowed or basis_adjusted",
    )

    # ── Method ────────────────────────────────────────────────────────
    cost_basis_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CostBasisMethod.FIFO.value,
        comment="Method used for cost basis (fifo / lifo / specific_id)",
    )

    def is_open(self) -> bool:
        """Return True if this lot is still open (not fully closed)."""
        return self.closed_at is None

    @property
    def closed_quantity(self) -> Decimal:
        """Number of units that have been sold from this lot."""
        return self.quantity - self.remaining_quantity

    def __repr__(self) -> str:
        return (
            f"<TaxLot id={self.id!r}"
            f" sec={self.security_id!r}"
            f" qty={self.quantity!r}"
            f" remaining={self.remaining_quantity!r}"
            f" cost={self.cost_basis_total!r}"
            f" closed={'open' if self.is_open() else 'closed'}>"
        )
