"""Tenant-scoped security audit log for connection lifecycle events.

Records every sensitive connection-management action (create, update,
test, pause, resume, account-selection changes, delete) so operators can
reconstruct *who did what to which connection, when*.  The ``detail``
JSONB payload is sanitised by the caller: it must never contain raw
credentials, tokens, or financial payloads — only non-secret identifiers,
labels, and status changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_sync.db import Base, created_at_ts, pk_uuid

#: Audit event kinds recorded for connection lifecycle actions.
AUDIT_CREATE = "create"
AUDIT_UPDATE = "update"
AUDIT_TEST = "test"
AUDIT_PAUSE = "pause"
AUDIT_RESUME = "resume"
AUDIT_ACCOUNTS = "select_accounts"
AUDIT_DELETE = "delete"
AUDIT_ACTIONS = {
    AUDIT_CREATE,
    AUDIT_UPDATE,
    AUDIT_TEST,
    AUDIT_PAUSE,
    AUDIT_RESUME,
    AUDIT_ACCOUNTS,
    AUDIT_DELETE,
}


class ConnectionAuditLog(Base):
    """One audit entry per sensitive connection-management action."""

    __tablename__ = "connection_audit_log"
    __table_args__: ClassVar = (
        Index(
            "ix_connection_audit_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_connection_audit_connection",
            "connection_id",
        ),
    )

    id: Mapped[str] = pk_uuid()

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Connection (credential) id this event refers to; kept as a "
            "plain string so the audit trail survives credential deletion"
        ),
    )
    provider_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Connector name, e.g. 'bunq', 'trading212'",
    )
    action: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=("create/update/test/pause/resume/select_accounts/delete"),
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
        comment=(
            "Sanitised event payload: labels, status transitions, account "
            "selection changes. Never contains secrets or financial data."
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
            f"<ConnectionAuditLog tenant={self.tenant_id!r} "
            f"provider={self.provider_key!r} action={self.action!r} "
            f"connection={self.connection_id!r}>"
        )
