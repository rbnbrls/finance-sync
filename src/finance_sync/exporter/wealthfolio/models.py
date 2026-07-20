"""ORM models for the Wealthfolio exporter.

ExportRun (shared with other exporters)
    Tracks each export attempt, reused from the main exporter models.

WealthfolioAccountMapping
    Persists the mapping between a finance-sync account and a Wealthfolio
    account. Created automatically on first export for each account pair.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid


class WealthfolioAccountMapping(Base):
    """Maps a finance-sync account to a Wealthfolio account.

    The Wealthfolio account is identified by its display name
    (human-readable).  CSV imports reference accounts by name.
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

    created_at = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<WealthfolioAccountMapping "
            f"acct={self.account_id!r} -> wf={self.wf_account_name!r}>"
        )
