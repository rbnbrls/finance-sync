"""Tenant-scoped household invitation model.

An invitation lets an admin add a second household member to the tenant.
The invite token is stored **hashed** (SHA-256 hex digest — the token is
32 bytes of CSPRNG entropy, so the digest is not reversible and enables
deterministic lookups, unlike a salted bcrypt hash) so a database leak
never yields usable invitation links; tokens are single-use and expire
after ``INVITATION_TTL_DAYS`` days.  The invite flow deliberately never
reveals whether the invited email already belongs to a tenant user —
the invite endpoint returns the same generic response either way, so an
admin cannot probe the user directory through it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid, updated_at_ts
from finance_sync.models.enums import InvitationStatus, UserRole

#: Default lifetime of a pending invitation (in days).
INVITATION_TTL_DAYS = 7

#: Status values valid at creation time.
CREATABLE_STATUSES = {InvitationStatus.PENDING}


class HouseholdInvitation(Base):
    """A single-use, expiring invitation to join a tenant's household."""

    __tablename__ = "household_invitations"
    __table_args__: ClassVar = (
        Index(
            "ix_household_invitations_tenant_status",
            "tenant_id",
            "status",
        ),
        Index("ix_household_invitations_token_hash", "token_hash"),
    )

    id: Mapped[str] = pk_uuid()
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        comment="Invited email address (lower-cased)",
    )
    token_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="SHA-256 hex digest of the single-use invite token",
    )
    role: Mapped[str] = mapped_column(
        String(32),
        default=UserRole.USER,
        nullable=False,
        comment="Role the invitee will receive on acceptance",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=InvitationStatus.PENDING,
        nullable=False,
        comment="pending/accepted/expired/revoked",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Invitation expiry (UTC)",
    )
    created_by: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="User id of the inviting admin (plain string, no FK)",
    )
    accepted_by: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="User id that accepted the invitation, when accepted",
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_ts()
    updated_at: Mapped[datetime] = updated_at_ts()

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def is_pending(self) -> bool:
        """Return True while the invitation can still be accepted."""
        return (
            self.status == InvitationStatus.PENDING
            and self.expires_at > datetime.now(UTC)
        )

    @staticmethod
    def default_expiry() -> datetime:
        """Return the default ``expires_at`` for a new invitation."""
        return datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS)

    def to_dict(self) -> dict[str, Any]:
        """Sanitised public representation (never exposes the token hash)."""
        return {
            "id": str(self.id),
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "expires_at": self.expires_at.isoformat(),
            "created_by": self.created_by,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

    def __repr__(self) -> str:
        return (
            f"<HouseholdInvitation id={self.id!r} email={self.email!r} "
            f"status={self.status!r} role={self.role!r}>"
        )
