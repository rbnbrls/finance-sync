"""Provider authentication performed before the pipeline session opens.

Authentication is provider network I/O.  Running it while the pipeline holds
a pooled database connection keeps that connection for the whole provider
round trip, which can starve a small worker pool (see the Trading212
pool-exhaustion fix).  Hoisting it out of the pipeline must not change what
callers observe on failure, so a failed authentication is recorded as a
``FAILED`` ``SyncRun`` (with its category and message) and reported as the
``FAILED`` ``SyncResult`` the pipeline would have returned.  The pipeline
never started, so there are no resource rows to roll back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from finance_sync.connectors.exceptions import ConnectorError, RateLimitError
from finance_sync.models.enums import SyncRunStatus
from finance_sync.observability.glitchtip import capture_connector_exception
from finance_sync.services.incident_reporting import report_connector_failure
from finance_sync.sync.errors import (
    categorize_sync_error,
    classify_sync_error,
    safe_sync_error_message,
)
from finance_sync.sync.results import SyncResult
from finance_sync.sync.sync_run import record_failed_sync_run

if TYPE_CHECKING:
    import structlog
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finance_sync.connectors.base import Connector


async def authenticate_before_pipeline(
    connector: Connector,
    *,
    provider_type: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: object | None,
    log: structlog.BoundLogger,
    connection_id: str | None = None,
) -> SyncResult | None:
    """Authenticate a connector before the pipeline session is acquired.

    Returns ``None`` when authentication succeeded; otherwise the ``FAILED``
    ``SyncResult`` the pipeline would have returned, with the failed run
    already persisted.
    """
    started = datetime.now(UTC)
    try:
        await connector.authenticate()
    except Exception as exc:
        end_ts = datetime.now(UTC)
        await report_connector_failure(
            settings,
            exc,
            connector=provider_type,
            operation="authenticate",
            connection_id=connection_id,
            fallback_capture=capture_connector_exception,
        )
        rate_limited = isinstance(exc, RateLimitError)
        category = categorize_sync_error(exc)
        retry_after_at = (
            end_ts + timedelta(seconds=exc.retry_after)
            if rate_limited and exc.retry_after is not None
            else None
        )
        # Connector errors already carry an operator-facing message; any other
        # exception is bounded by the shared safe-message helper.
        message = (
            str(exc)
            if isinstance(exc, ConnectorError)
            else safe_sync_error_message(exc)
        )
        await record_failed_sync_run(
            session_factory,
            connector=provider_type,
            connection_id=connection_id,
            error_message=message,
            error_category=category,
            retry_after_at=retry_after_at,
            rate_limit_attempts=1 if rate_limited else 0,
            rate_limit_scope="connection" if rate_limited else None,
            last_http_status=429 if rate_limited else None,
        )
        return SyncResult(
            status=SyncRunStatus.FAILED,
            accounts_synced=0,
            transactions_synced=0,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=message,
            error_type=type(exc).__name__,
            error_category=category,
            error_kind=classify_sync_error(exc).value,
            retry_after_at=retry_after_at,
            rate_limit_scope="connection" if rate_limited else None,
            rate_limit_attempts=1 if rate_limited else 0,
            duration_s=(end_ts - started).total_seconds(),
        )

    log.debug("authenticated")
    return None
