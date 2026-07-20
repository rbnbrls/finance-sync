"""Authentication and RBAC user model."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_sync.db import Base, pk_uuid
from finance_sync.models.enums import UserRole
from finance_sync.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from finance_sync.models.tenant import Tenant


class User(TimestampMixin, Base):
    """A tenant-scoped user with RBAC roles."""

    __tablename__ = "users"
    __table_args__: ClassVar = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        String(32), default=UserRole.VIEWER, nullable=False
    )

    # ── relationships ────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship(
        "Tenant", back_populates="users", lazy="joined"
    )
