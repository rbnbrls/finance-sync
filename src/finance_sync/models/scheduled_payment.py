"""Scheduled / recurring payment model.

Represents a payment template that executes on a recurring schedule
(standing order, direct debit mandate, subscription, etc.).  Each
execution of the schedule produces a regular Transaction record.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import ScheduleFrequency, ScheduleStatus
from finance_sync.models.mixins import TimestampMixin


class ScheduledPayment(TimestampMixin, Base):
    """A scheduled / recurring payment template.

    Each schedule defines the payment amount, counterparty, and recurrence
    pattern.  Individual executions produce Transaction records linked
    back to this schedule via ``provider_metadata``.
    """

    __tablename__ = "scheduled_payments"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "provider_key",
            "external_schedule_id",
            name="uq_scheduled_payments_provider",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    provider_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Ingestion connector name"
    )
    external_schedule_id: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="Provider's schedule identifier"
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

    frequency: Mapped[ScheduleFrequency] = mapped_column(
        String(32),
        nullable=False,
        comment="Recurrence frequency",
    )
    interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Every N units of frequency (e.g. every 2 months)",
    )
    next_execution_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Next scheduled execution date",
    )
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Recurrence end date (optional)",
    )
    max_executions: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum number of executions (optional)",
    )
    execution_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="Number of times executed"
    )

    counterparty_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Counterparty / merchant name"
    )
    counterparty_iban: Mapped[str | None] = mapped_column(
        String(34), nullable=True, comment="Counterparty IBAN"
    )
    description: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="Payment description / reference"
    )

    status: Mapped[ScheduleStatus] = mapped_column(
        String(32),
        default=ScheduleStatus.ACTIVE,
        nullable=False,
        comment="active/paused/completed/cancelled/failed",
    )

    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduledPayment id={self.id!r} amount={self.amount!r} "
            f"freq={self.frequency!r} status={self.status!r}>"
        )
