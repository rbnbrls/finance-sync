"""Tenant-scoped security audit log for connection lifecycle actions.

Every sensitive connection-management action (create, update, test,
pause, resume, account-selection changes, delete) is recorded with a
sanitised detail payload.  Callers must never pass raw credentials or
financial payloads into :func:`log_connection_event` — the service
additionally applies the shared secret-redaction helpers as a defence in
depth, so even a stray secret value in a label or detail cannot reach
the audit table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from finance_sync.models import ConnectionAuditLog
from finance_sync.models.connection_audit_log import (
    AUDIT_ACCOUNTS,
    AUDIT_ACTIONS,
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_PAUSE,
    AUDIT_RESUME,
    AUDIT_SYNC,
    AUDIT_TEST,
    AUDIT_UPDATE,
)
from finance_sync.utils.redaction import redact_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Max detail payload size (stringified) to keep the table lean.
_MAX_DETAIL_CHARS = 2000

__all__ = [
    "AUDIT_ACCOUNTS",
    "AUDIT_ACTIONS",
    "AUDIT_CREATE",
    "AUDIT_DELETE",
    "AUDIT_PAUSE",
    "AUDIT_RESUME",
    "AUDIT_SYNC",
    "AUDIT_TEST",
    "AUDIT_UPDATE",
    "list_connection_audit_events",
    "log_connection_event",
]


async def log_connection_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    provider_key: str,
    connection_id: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    secrets: list[str] | None = None,
    flush: bool = True,
) -> ConnectionAuditLog:
    """Append one sanitised audit entry to the tenant's audit trail.

    The *detail* payload is JSON-serialised; string values are redacted
    with the shared secret scrubber (defence in depth).  The record is
    added to *session* and flushed when *flush* is true.
    """
    sanitised_detail: dict[str, Any] = {}
    secrets_list = secrets or []
    for key, value in (detail or {}).items():
        if isinstance(value, str):
            sanitised_detail[key] = redact_text(value, secrets_list)
        elif isinstance(value, list):
            scrubbed: list[Any] = []
            for item in cast("list[Any]", value):
                if isinstance(item, str):
                    scrubbed.append(redact_text(item, secrets_list))
                else:
                    scrubbed.append(item)
            sanitised_detail[key] = scrubbed
        else:
            sanitised_detail[key] = value

    entry = ConnectionAuditLog(
        tenant_id=tenant_id,
        connection_id=connection_id,
        provider_key=provider_key,
        action=action,
        detail=sanitised_detail,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
    session.add(entry)
    if flush:
        await session.flush()
    return entry


async def list_connection_audit_events(
    session: AsyncSession,
    *,
    tenant_id: str,
    connection_id: str | None = None,
    provider_key: str | None = None,
    limit: int = 100,
) -> list[ConnectionAuditLog]:
    """List the tenant's audit entries, newest first."""
    stmt = (
        select(ConnectionAuditLog)
        .where(ConnectionAuditLog.tenant_id == tenant_id)
        .order_by(ConnectionAuditLog.created_at.desc())
        .limit(limit)
    )
    if connection_id is not None:
        stmt = stmt.where(  # type: ignore[attr-defined]
            ConnectionAuditLog.connection_id == connection_id  # type: ignore[attr-defined]
        )
    if provider_key is not None:
        stmt = stmt.where(  # type: ignore[attr-defined]
            ConnectionAuditLog.provider_key == provider_key  # type: ignore[attr-defined]
        )
    rows = await session.scalars(stmt)
    return list(rows.all())
