"""Error classification and safe messages for sync boundaries."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from finance_sync.connectors.exceptions import (
    ConnectorError,
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.control_plane_contract import CONTROL_PLANE_ERROR_CATEGORIES

# Backwards-compatible name for sync callers; the contract itself has one
# canonical source in ``control_plane_contract``.
SYNC_ERROR_CATEGORIES = set(CONTROL_PLANE_ERROR_CATEGORIES)


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


def categorize_sync_error(error: BaseException) -> str:
    """Return the stable control-plane category for an operational error."""
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, TransientError):
        return "provider_unavailable"
    if isinstance(error, PermanentError):
        message = str(error).lower()
        if "incompatible" in message:
            return "incompatible"
        if any(
            token in message
            for token in ("expired", "revoked", "reauth", "401", "403")
        ):
            return "reauth_required"
        if any(token in message for token in ("expired", "token expired")):
            return "token_expired"
        if any(token in message for token in ("auth", "credential", "token")):
            return "authentication"
        if any(token in message for token in ("map", "security", "instrument")):
            return "data_mapping"
        if any(token in message for token in ("valid", "malformed", "invalid")):
            return "validation"
        return "unknown"
    if isinstance(error, ConnectorError):
        return "provider_unavailable"
    if error.__class__.__module__.startswith("sqlalchemy"):
        return "database"
    return "unknown"


def categorize_export_error(message: str | None) -> str | None:
    """Normalize persisted exporter messages without exposing raw details."""
    if not message:
        return None
    text = message.lower()
    if any(
        token in text
        for token in ("expired", "revoked", "reauth")
    ):
        return "reauth_required"
    if any(
        token in text for token in ("auth", "credential", "token", "401", "403")
    ):
        return "authentication"
    if any(token in text for token in ("rate limit", "429", "too many")):
        return "rate_limited"
    if any(token in text for token in ("mapping", "security", "instrument")):
        return "data_mapping"
    if any(token in text for token in ("invalid", "validation", "malformed")):
        return "validation"
    if any(token in text for token in ("database", "sqlalchemy", "postgres")):
        return "database"
    return "provider_unavailable"
