"""SyncRun lifecycle helpers.

Provides thin functions for creating and completing ``SyncRun``
records inside a UnitOfWork transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_sync.models import SyncRun
from finance_sync.models.enums import SyncRunStatus


async def start_sync_run(
    uow: object,
    *,
    connector: str,
    connection_id: str | None = None,
) -> SyncRun:
    """Create a new ``SyncRun`` record with status ``running``.

    The record is added to the session but not flushed — it commits
    atomically with the enclosing transaction.

    When *connection_id* is provided (multi-connection syncs) the run is
    scoped to that connection so per-connection runs stay traceable.

    Returns the created ``SyncRun`` instance.
    """
    run = SyncRun(
        connector=connector,
        connection_id=connection_id,
        status=SyncRunStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    # uow.session.add() — the caller provides a UoW with an active session
    uow.session.add(run)  # type: ignore[union-attr]
    return run


async def complete_sync_run(
    uow: object,
    run: SyncRun,
    *,
    status: SyncRunStatus = SyncRunStatus.COMPLETED,
    items_processed: int | None = None,
    error_message: str | None = None,
    error_category: str | None = None,
    cursor: datetime | None = None,
) -> SyncRun:
    """Mark a ``SyncRun`` as completed / failed.

    Updates the run in-place and flushes so the changes are visible to
    subsequent reads within the same transaction.

    ``cursor`` records the watermark the run advanced to — set only on
    success so a failed run never claims to have advanced the
    incremental sync position.
    """
    run.status = status
    run.completed_at = datetime.now(UTC)
    if items_processed is not None:
        run.items_processed = items_processed
    if error_message is not None:
        run.error_message = error_message
    if error_category is not None:
        run.error_category = error_category
    if cursor is not None:
        run.cursor = cursor
    await uow.session.flush()  # type: ignore[union-attr]
    return run
