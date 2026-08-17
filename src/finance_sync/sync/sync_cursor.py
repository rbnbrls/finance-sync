# pyright: basic
"""Sync cursor (watermark) persistence helpers.

Thin read/write helpers for the ``sync_cursor`` table, mirroring the
``sync/sync_run.py`` lifecycle-helper style.  Callers pass the active
``AsyncSession`` (usually ``uow.session``) so reads and writes
participate in the enclosing UnitOfWork transaction — a cursor is only
advanced when the whole sync run commits, and rolled back with it on
failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from finance_sync.models import SyncCursor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Resource key used by the bunq cards / scheduled-payments pipeline.
RESOURCE_CARD_TRANSACTIONS = "card_transactions"


async def get_connector_cursors(
    session: AsyncSession,
    *,
    tenant_id: str,
    connector: str,
    connection_id: str | None = None,
) -> dict[str, datetime]:
    """Return ``{resource: cursor}`` for a connector's stored watermarks.

    The main pipeline keys resources by the external account id, so a
    per-account resume position is looked up directly; accounts without
    a stored cursor (e.g. newly added) fall back to the run-level
    ``since`` (explicit backfill or the 90-day first-sync default).

    When *connection_id* is provided the cursors are scoped to that
    connection so identical external ids from two connections resume
    independently; when omitted (legacy single-connection syncs) only
    rows without a connection scope are returned.
    """
    stmt = select(SyncCursor).where(
        SyncCursor.tenant_id == tenant_id,
        SyncCursor.connector == connector,
    )
    if connection_id is not None:
        stmt = stmt.where(SyncCursor.connection_id == connection_id)
    else:
        stmt = stmt.where(SyncCursor.connection_id.is_(None))
    rows = await session.scalars(stmt)
    return {row.resource: row.cursor for row in rows}


async def get_cursor(
    session: AsyncSession,
    *,
    tenant_id: str,
    connector: str,
    resource: str,
    connection_id: str | None = None,
) -> datetime | None:
    """Return the stored cursor for one resource, or ``None``."""
    cursors = await get_connector_cursors(
        session,
        tenant_id=tenant_id,
        connector=connector,
        connection_id=connection_id,
    )
    return cursors.get(resource)


async def upsert_sync_cursor(
    session: AsyncSession,
    *,
    tenant_id: str,
    connector: str,
    resource: str,
    cursor: datetime,
    connection_id: str | None = None,
) -> SyncCursor:
    """Create or update the watermark for one ``(connector, resource)``.

    Idempotent: the ``(tenant_id, connector, resource)`` unique
    constraint means repeated runs update the row in place.  When
    *connection_id* is provided, the watermark is scoped to that
    connection (and the connection scope is persisted on the row);
    legacy syncs without a connection scope keep ``NULL``.
    """
    filters = [
        SyncCursor.tenant_id == tenant_id,
        SyncCursor.connector == connector,
        SyncCursor.resource == resource,
    ]
    if connection_id is not None:
        filters.append(SyncCursor.connection_id == connection_id)
    else:
        filters.append(SyncCursor.connection_id.is_(None))
    row = await session.scalar(select(SyncCursor).where(*filters))
    if row is None:
        row = SyncCursor(
            tenant_id=tenant_id,
            connector=connector,
            connection_id=connection_id,
            resource=resource,
            cursor=cursor,
        )
        session.add(row)
    else:
        row.cursor = cursor
    await session.flush()
    return row
