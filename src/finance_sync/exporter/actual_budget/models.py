"""ORM models for the Actual Budget exporter.

ExportRun
    Tracks each export attempt (analogous to SyncRun for ingestion).
    Shared with other exporters — defined in ``exporter.models``.

ActualBudgetAccountMapping
    Persists the mapping between a finance-sync account and an Actual Budget
    account (identified by its internal UUID).  Created automatically on
    first export for each account pair.

ExportDelivery
    Tracks the delivery cursor per account for idempotent export.
    Records the last-successfully-exported transaction ID and timestamp
    so that a subsequent run can pick up where it left off without
    re-exporting already-delivered transactions.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime by SQLAlchemy
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


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


class ExportDelivery(Base):
    """Idempotency cursor for export deliveries.

    Records the last successfully exported transaction per account
    so that the next export run can resume from that point without
    re-processing already-delivered transactions.
    """

    __tablename__ = "export_deliveries"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_export_delivery_account",
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

    # ── Delivery cursor ──────────────────────────────────────────────
    last_exported_transaction_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="ID of the last successfully exported transaction",
    )
    last_exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of the last successful export for this account",
    )
    last_cursor: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Provider cursor / checkpoint token for resume",
    )

    # ── Run tracking ─────────────────────────────────────────────────
    export_run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="ID of the ExportRun that last updated this cursor",
    )

    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ExportDelivery account={self.account_id!r} "
            f"last_tx={self.last_exported_transaction_id!r} "
            f"at={self.last_exported_at!r}>"
        )
