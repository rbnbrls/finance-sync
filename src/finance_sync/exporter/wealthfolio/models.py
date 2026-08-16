"""ORM models for the Wealthfolio exporter.

ExportRun (shared with other exporters)
    Tracks each export attempt, reused from the main exporter models.

WealthfolioAccountMapping
    Persists the mapping between a finance-sync account and a Wealthfolio
    account. Created automatically on first export for each account pair.

WealthfolioDelivery
    Tracks the delivery cursor per account for idempotent push resume.
    Records the last-successfully-pushed transaction ID and timestamp so
    a subsequent push (or a retry after a partial failure) can pick up
    where it left off without re-pushing already-delivered transactions.
    Mirrors ``ExportDelivery`` for the Actual Budget exporter, but is kept
    as a separate table so both exporters can maintain independent cursors
    for the same finance-sync account.
"""

from __future__ import annotations

from datetime import (
    datetime,
)
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class WealthfolioAccountMapping(Base):
    """Maps a finance-sync account to a Wealthfolio account.

    The remote UUID is authoritative. The display name is retained for
    operator visibility and backwards-compatible CSV exports.
    """

    __tablename__ = "wealthfolio_account_mappings"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_wealthfolio_mapping_account",
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── Finance-sync side ───────────────────────────────────────────
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        comment="finance-sync account UUID",
    )

    # ── Wealthfolio side ───────────────────────────────────────────
    wf_account_name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Wealthfolio account display name",
    )
    wf_account_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Wealthfolio account UUID returned by its API",
    )
    provider_account_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        comment="Stable finance-sync identity stored in Wealthfolio",
    )

    created_at = created_at_ts()
    updated_at = updated_at_ts()

    def __repr__(self) -> str:
        return (
            f"<WealthfolioAccountMapping "
            f"acct={self.account_id!r} -> wf={self.wf_account_name!r}>"
        )


class WealthfolioDelivery(Base):
    """Idempotency cursor for Wealthfolio push deliveries.

    Records the last successfully pushed transaction per account so that
    the next push (or a retry after a partial failure) resumes from that
    point without re-pushing already-delivered transactions.
    """

    __tablename__ = "wealthfolio_deliveries"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            name="uq_wealthfolio_delivery_account",
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
        comment="ID of the last successfully pushed transaction",
    )
    last_exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of the last successful push for this account",
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
            f"<WealthfolioDelivery account={self.account_id!r} "
            f"last_tx={self.last_exported_transaction_id!r} "
            f"at={self.last_exported_at!r}>"
        )
