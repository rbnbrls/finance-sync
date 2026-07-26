"""SQLAlchemy ORM models for Actual Budget exporter integration.

Stores the mapping between finance-sync accounts and Actual Budget
accounts, as well as cursor state for incremental export — allowing
a subsequent run to pick up where it left off without re-exporting
already-delivered transactions.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts


class ActualBudgetAccountMapping(Base):
    """Maps a finance-sync account to an Actual Budget account.

    The ``ab_account_id`` is the internal UUID that Actual Budget
    assigns to the account (not the human-readable name).
    """

    __tablename__ = "ab_account_mappings"
    __table_args__: ClassVar = (
        UniqueConstraint("tenant_id", "ab_account_id", name="uq_ab_acct_map"),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    ab_account_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )

    created_at: Mapped[datetime] = created_at_ts()
    updated_at: Mapped[datetime] = updated_at_ts()


class ActualBudgetCursor(Base):
    """Tracks the last transaction exported to Actual Budget.

    Used for incremental export — only transactions newer than
    ``last_exported_at`` will be included in the next run.
    """

    __tablename__ = "ab_export_cursors"

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    last_exported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Timestamp of the most recently exported transaction",
    )

    created_at: Mapped[datetime] = created_at_ts()
    updated_at: Mapped[datetime] = updated_at_ts()
