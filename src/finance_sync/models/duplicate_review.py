"""Audited user decisions for duplicate-transaction findings."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid

DuplicateReviewDecision = Literal["keep_a", "keep_b", "keep_both"]


class DuplicateReview(Base):
    """One durable decision for an unordered pair of transactions."""

    __tablename__ = "duplicate_reviews"
    __table_args__: ClassVar = (
        UniqueConstraint(
            "tenant_id", "pair_key", name="uq_duplicate_review_pair"
        ),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    transaction_id_a: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    transaction_id_b: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    pair_key: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    kept_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = created_at_ts()
