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
) -> dict[str, datetime]:
    """Return ``{resource: cursor}`` for a connector's stored watermarks.

    The main pipeline keys resources by the external account id, so a
    per-account resume position is looked up directly; accounts without
    a stored cursor (e.g. newly added) fall back to the run-level
    ``since`` (explicit backfill or the 90-day first-sync default).
    """
    rows = await session.scalars(
        select(SyncCursor).where(
            SyncCursor.tenant_id == tenant_id,
            SyncCursor.connector == connector,
        )
    )
    return {row.resource: row.cursor for row in rows}


async def get_cursor(
    session: AsyncSession,
    *,
    tenant_id: str,
    connector: str,
    resource: str,
) -> datetime | None:
    """Return the stored cursor for one resource, or ``None``."""
    cursors = await get_connector_cursors(
        session, tenant_id=tenant_id, connector=connector
    )
    return cursors.get(resource)


async def upsert_sync_cursor(
    session: AsyncSession,
    *,
    tenant_id: str,
    connector: str,
    resource: str,
    cursor: datetime,
) -> SyncCursor:
    """Create or update the watermark for one ``(connector, resource)``.

    Idempotent: the ``(tenant_id, connector, resource)`` unique
    constraint means repeated runs update the row in place.
    """
    row = await session.scalar(
        select(SyncCursor).where(
            SyncCursor.tenant_id == tenant_id,
            SyncCursor.connector == connector,
            SyncCursor.resource == resource,
        )
    )
    if row is None:
        row = SyncCursor(
            tenant_id=tenant_id,
            connector=connector,
            resource=resource,
            cursor=cursor,
        )
        session.add(row)
    else:
        row.cursor = cursor
    await session.flush()
    return row
