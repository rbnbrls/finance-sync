"""Reconciliation models for detecting missing / duplicate transactions.

ReconciliationRun tracks a single reconciliation run (a batch analysis
of transaction data). ReconciliationResult holds each individual finding
such as a detected duplicate, a cross-connector gap, or an amount
mismatch.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid
from finance_sync.models.enums import (
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
)


class ReconciliationRun(Base):
    """Tracks a reconciliation analysis run.

    Each run represents a batch analysis of transaction data within a
    given scope (optional account IDs and date range) that detects
    duplicates, missing transactions, and cross-connector gaps.
    """

    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── State ─────────────────────────────────────────────────────
    status: Mapped[ReconciliationRunStatus] = mapped_column(
        String(16),
        default=ReconciliationRunStatus.RUNNING,
        nullable=False,
        comment="'running', 'completed', 'failed', 'cancelled'",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Scope ──────────────────────────────────────────────────────
    scope: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Run scope: {account_ids: [..], date_from: '..', date_to: '..'}"
        ),
    )

    # ── Outcome ────────────────────────────────────────────────────
    finding_count: Mapped[int | None] = mapped_column(
        nullable=True, comment="Total number of findings in this run"
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Summary stats: {duplicates: N, missing: N, "
            "cross_connector: N, by_severity: {info: N, ...}}"
        ),
    )
    error_message: Mapped[str | None] = mapped_column(nullable=True)

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRun id={self.id!r} "
            f"status={self.status!r} findings={self.finding_count}>"
        )


class ReconciliationResult(Base):
    """A single finding from a reconciliation analysis run.

    Each result describes one detected issue: a potential duplicate
    transaction, a gap where a transaction is expected but missing,
    or a cross-connector mismatch between providers.
    """

    __tablename__ = "reconciliation_results"
    __table_args__: ClassVar = (
        {"comment": "Individual reconciliation findings per run"},
    )

    id: Mapped[str] = pk_uuid()
    run_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )

    # ── Classification ────────────────────────────────────────────
    kind: Mapped[ReconciliationResultKind] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "'duplicate_transaction', 'missing_transaction', "
            "'cross_connector_mismatch', 'amount_mismatch'"
        ),
    )
    severity: Mapped[ReconciliationSeverity] = mapped_column(
        String(16),
        nullable=False,
        default=ReconciliationSeverity.WARNING,
        comment="'info', 'warning', 'error'",
    )

    # ── Provider / account context ─────────────────────────────────
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Primary connector involved"
    )
    other_provider_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Secondary connector (for cross-connector)",
    )

    # ── Transaction references ─────────────────────────────────────
    transaction_id_a: Mapped[str | None] = mapped_column(
        nullable=True, comment="First (or only) transaction involved"
    )
    transaction_id_b: Mapped[str | None] = mapped_column(
        nullable=True, comment="Second transaction (for duplicates/mismatches)"
    )
    external_transaction_id_a: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Provider ID of first transaction"
    )
    external_transaction_id_b: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Provider ID of second transaction"
    )

    # ── Financial details ──────────────────────────────────────────
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Transaction amount (if applicable)",
    )
    other_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8),
        nullable=True,
        comment="Other amount for comparison (mismatch context)",
    )
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Message / metadata ─────────────────────────────────────────
    description: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Human-readable finding summary"
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="Extra context (score, diff, etc.)"
    )

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<ReconciliationResult id={self.id!r} kind={self.kind!r} "
            f"severity={self.severity!r}>"
        )
