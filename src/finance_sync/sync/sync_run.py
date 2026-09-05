"""SyncRun lifecycle helpers.

Provides thin functions for creating and completing ``SyncRun``
records inside a UnitOfWork transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from finance_sync.models import SyncRun
from finance_sync.models.enums import SyncRunStatus


class SyncAlreadyRunningError(RuntimeError):
    """Raised when a connection already owns an active sync run."""


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
    if connection_id is not None:
        active = await uow.session.scalar(  # type: ignore[union-attr]
            select(SyncRun.id)
            .where(
                SyncRun.connection_id == connection_id,
                SyncRun.status == SyncRunStatus.RUNNING,
            )
            .limit(1)
        )
        if active is not None:
            message = f"Connection already has sync run {active} in progress"
            raise SyncAlreadyRunningError(message)

    run = SyncRun(
        connector=connector,
        connection_id=connection_id,
        status=SyncRunStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    # uow.session.add() — the caller provides a UoW with an active session
    uow.session.add(run)  # type: ignore[union-attr]
    return run


async def recover_stale_sync_runs(
    session: object,
    *,
    connection_id: str,
    stale_after_minutes: int,
) -> int:
    """Close runs left behind by a crashed/restarted worker.

    Only runs older than the explicit safety window are recovered; a normal
    long-running provider sync is therefore not interrupted.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    result = await session.execute(  # type: ignore[union-attr]
        update(SyncRun)
        .where(
            SyncRun.connection_id == connection_id,
            SyncRun.status == SyncRunStatus.RUNNING,
            SyncRun.started_at < cutoff,
        )
        .values(
            status=SyncRunStatus.FAILED,
            completed_at=datetime.now(UTC),
            error_message=(
                "Run automatisch beëindigd: worker was niet meer actief"
            ),
            error_category="stale_run",
        )
    )
    return int(result.rowcount or 0)


async def complete_sync_run(
    uow: object,
    run: SyncRun,
    *,
    status: SyncRunStatus = SyncRunStatus.COMPLETED,
    items_processed: int | None = None,
    error_message: str | None = None,
    error_category: str | None = None,
    cursor: datetime | None = None,
    retry_after_at: datetime | None = None,
    rate_limit_attempts: int = 0,
    rate_limit_scope: str | None = None,
    last_http_status: int | None = None,
    report: dict[str, int] | None = None,
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
    if retry_after_at is not None:
        run.retry_after_at = retry_after_at
    if rate_limit_attempts:
        run.rate_limit_attempts = rate_limit_attempts
    if rate_limit_scope is not None:
        run.rate_limit_scope = rate_limit_scope
    if last_http_status is not None:
        run.last_http_status = last_http_status
    if report is not None:
        run.report = dict(report)
    await uow.session.flush()  # type: ignore[union-attr]
    return run
