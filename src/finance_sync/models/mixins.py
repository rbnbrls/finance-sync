"""Reusable SQLAlchemy ORM mixins.

Provides ``TimestampMixin`` (automatic ``created_at`` / ``updated_at``)
and ``TenantAwareMixin`` (``tenant_id`` column with index).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` auto-timestamp columns.

    Usage::

        class MyModel(TimestampMixin, Base):
            __tablename__ = "my_table"
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class TenantAwareMixin:
    """Adds a ``tenant_id`` foreign-key column with an index.

    The column is non-nullable UUID.  The concrete model is responsible
    for adding the actual ``ForeignKey`` constraint.
    """

    @declared_attr
    def tenant_id(self) -> Mapped[Any]:
        return mapped_column(UUID(as_uuid=True), nullable=False, index=True)
