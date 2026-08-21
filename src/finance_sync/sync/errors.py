"""Error classification and safe messages for sync boundaries."""

from __future__ import annotations

from enum import StrEnum

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    TransientError,
)


class SyncErrorKind(StrEnum):
    """Stable categories used by logs and operational metrics."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    INTERNAL = "internal"


def classify_sync_error(error: BaseException) -> SyncErrorKind:
    """Map connector failures to a stable operational category."""
    if isinstance(error, TransientError):
        return SyncErrorKind.TRANSIENT
    if isinstance(error, PermanentError | ConnectorError):
        return SyncErrorKind.PERMANENT
    return SyncErrorKind.INTERNAL


def safe_sync_error_message(error: BaseException) -> str:
    """Return a bounded message suitable for a SyncRun/API response."""
    if isinstance(error, ConnectorError):
        return str(error)[:2048] or "Connector sync failed"
    return "Sync failed due to an internal error"
