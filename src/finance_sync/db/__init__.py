"""Database layer — declarative base, types, repository, and UoW.

Usage
-----
::

    from finance_sync.db import Base
    from finance_sync.db.types import CurrencyCode, MonetaryAmount
    from finance_sync.db.repository import Repository
    from finance_sync.db.repositories import (
        AccountRepository, TenantRepository, ...
    )
    from finance_sync.db.uow import UnitOfWork
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, MetaData, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

from finance_sync.db.types import CurrencyCode, MonetaryAmount

__all__ = [
    "Base",
    "CurrencyCode",
    "MonetaryAmount",
    "UTCDateTime",
    "created_at_ts",
    "fk_uuid",
    "metadata",
    "pk_uuid",
    "tenant_fk",
    "updated_at_ts",
]

# ── Naming convention for constraints / indexes ─────────────────────
convention: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)  # type: ignore[arg-type]


class Base(DeclarativeBase):
    """Abstract declarative base for all finance-sync models."""

    metadata = metadata


# ── Custom column types ──────────────────────────────────────────────


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime that stores/retrieves as UTC-aware ``timestamptz``."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(  # type: ignore[override]
        self,
        value: datetime | None,
        _dialect: Any,
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


# ── Common column factories ──────────────────────────────────────────


def pk_uuid() -> Any:
    """Return a Mapped annotation for a UUID primary key."""
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


def fk_uuid() -> Any:
    """Return a Mapped annotation for a non-nullable UUID foreign key.

    Callers must also add ``ForeignKey(...)`` separately::

        tenant_id: Mapped[uuid.UUID] = fk_uuid()
    """
    return mapped_column(UUID(as_uuid=True), nullable=False)


def tenant_fk() -> Any:
    """Shortcut for ``tenant_id`` foreign-key column (always non-null)."""
    return mapped_column(UUID(as_uuid=True), nullable=False, index=True)


def created_at_ts() -> Any:
    """Auto-set ``created_at`` timestamp."""
    return mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


def updated_at_ts() -> Any:
    """Auto-set ``updated_at`` timestamp (updated on row change)."""
    return mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
