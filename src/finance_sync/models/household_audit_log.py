"""Tenant-scoped security audit log for household management actions.

Records every sensitive household action — invite, revoke, accept,
role change, member removal, account share/unshare, account claim and
export-quarantine decisions — so operators can reconstruct *who did
what, when*.  The ``detail`` JSONB payload is sanitised by the caller:
it must never contain financial payloads (amounts, balances,
descriptions) or secrets (tokens, password hashes).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid

# ── Audit event kinds for household actions ────────────────────────────

AUDIT_INVITE = "invite"
AUDIT_REVOKE_INVITE = "revoke_invitation"
AUDIT_ACCEPT_INVITE = "accept_invitation"
AUDIT_ROLE_CHANGE = "role_change"
AUDIT_REMOVE_MEMBER = "remove_member"
AUDIT_ACCOUNT_SHARE = "account_share"
AUDIT_ACCOUNT_UNSHARE = "account_unshare"
AUDIT_ACCOUNT_CLAIM = "account_claim"
AUDIT_ACCOUNT_EXPORT_QUARANTINE = "account_export_quarantine"

HOUSEHOLD_AUDIT_ACTIONS = {
    AUDIT_INVITE,
    AUDIT_REVOKE_INVITE,
    AUDIT_ACCEPT_INVITE,
    AUDIT_ROLE_CHANGE,
    AUDIT_REMOVE_MEMBER,
    AUDIT_ACCOUNT_SHARE,
    AUDIT_ACCOUNT_UNSHARE,
    AUDIT_ACCOUNT_CLAIM,
    AUDIT_ACCOUNT_EXPORT_QUARANTINE,
}


class HouseholdAuditLog(Base):
    """One audit entry per sensitive household-management action."""

    __tablename__ = "household_audit_log"
    __table_args__: ClassVar = (
        Index(
            "ix_household_audit_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        comment=(
            "invite/revoke_invitation/accept_invitation/role_change/"
            "remove_member/account_share/account_unshare/account_claim/"
            "account_export_quarantine"
        ),
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
        comment=(
            "Sanitised event payload: user ids, emails, roles, account "
            "ids, visibility transitions. Never contains financial data "
            "or secrets."
        ),
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="User or API-key id that performed the action, when known",
    )
    actor_role: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Role of the performing principal (admin/user), when known",
    )

    created_at: Mapped[datetime] = created_at_ts()

    def __repr__(self) -> str:
        return (
            f"<HouseholdAuditLog tenant={self.tenant_id!r} "
            f"action={self.action!r}>"
        )
