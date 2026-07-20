"""Multi-tenant foundation model."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_sync.db import Base, pk_uuid
from finance_sync.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from finance_sync.models.user import User


class Tenant(TimestampMixin, Base):
    """A logical ownership boundary.

    Every resource belongs to exactly one tenant.  Multi-tenancy is
    implemented via a shared-database, shared-schema strategy — every
    financial table carries a ``tenant_id`` column.
    """

    __tablename__ = "tenants"
    __table_args__: ClassVar = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    id: Mapped[str] = pk_uuid()
    slug: Mapped[str] = mapped_column(
        String(128), unique=False, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    # ── relationships ────────────────────────────────────────────────
    users: Mapped[list[User]] = relationship(
        "User", back_populates="tenant", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id!r} slug={self.slug!r}>"
