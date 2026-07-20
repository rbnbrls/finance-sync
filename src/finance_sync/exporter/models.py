"""ORM models for the Actual Budget exporter.

ExportRun
    Tracks each export attempt (analogous to SyncRun for ingestion).

ActualBudgetMapping
    Persists the mapping between a finance-sync account and an Actual Budget
    account (identified by its internal UUID).  Created automatically on
    first export for each account pair.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class ExportRun(Base):
    """Tracks a single export run to Actual Budget.

    Each run stores its outcome so downstream alerting / dashboards
    can observe export health.
    """

    __tablename__ = "export_runs"

    id: Mapped[str] = pk_uuid()

    # ── State ────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        default="running",
        nullable=False,
        comment="'running', 'completed', 'failed', 'cancelled'",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Outcome ──────────────────────────────────────────────────────
    transactions_attempted: Mapped[int | None] = mapped_column(nullable=True)
    transactions_exported: Mapped[int | None] = mapped_column(nullable=True)
    transactions_failed: Mapped[int | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ExportRun id={self.id!r} status={self.status!r} "
            f"exported={self.transactions_exported!r}>"
        )


class ActualBudgetAccountMapping(Base):
    """Maps a finance-sync account to an Actual Budget account.

    The ``ab_account_id`` is the internal UUID that Actual Budget
    assigns to the account (not the human-readable name).
    """

    __tablename__ = "ab_account_mappings"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_ab_mapping_account",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── Finance-sync side ────────────────────────────────────────────
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="finance-sync account UUID",
    )

    # ── Actual Budget side ───────────────────────────────────────────
    ab_account_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Actual Budget internal account UUID",
    )
    ab_account_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Actual Budget account display name (cached)",
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ActualBudgetAccountMapping "
            f"acct={self.account_id!r} -> ab={self.ab_account_name!r}>"
        )
