"""Error classification and safe messages for sync boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

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
    if isinstance(error, InvalidSinceError):
        # A rejected ``since`` parameter is a caller/contract error, not
        # an internal fault — retrying the same request cannot fix it.
        return SyncErrorKind.PERMANENT
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
    if isinstance(error, InvalidSinceError):
        return "validation"
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
        if any(
            token in message
            for token in ("valid", "malformed", "invalid", "rejected", "400")
        ):
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
    if any(token in text for token in ("expired", "revoked", "reauth")):
        return "reauth_required"
    if any(token in text for token in ("expired", "token expired")):
        return "token_expired"
    if any(token in text for token in ("auth", "credential", "token")):
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


# ── ``since`` parameter validation ─────────────────────────────────────


class InvalidSinceError(ValueError):
    """Raised when a ``since`` parameter cannot be interpreted as a datetime.

    The message intentionally never echoes the offending raw value: it may
    originate from user input or a stored cursor and must not be logged or
    returned verbatim (it could carry sensitive fragments).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        message = (
            f"Invalid 'since' parameter: {reason}. "
            "Expected an ISO-8601 datetime (e.g. 2026-05-29T13:04:07Z) "
            "or an RFC 3339 / ISO datetime; a naive value is interpreted "
            "as UTC."
        )
        super().__init__(message)


def validate_since(
    value: datetime | str | None,
    *,
    default: datetime | None = None,
) -> datetime:
    """Normalise a ``since`` parameter to an aware UTC :class:`datetime`.

    ``since`` values can arrive from several callers (REST query params,
    MCP tool input, scheduler cursor state, internal defaults).  Instead
    of letting a malformed value explode deep inside a provider-specific
    connector (``strftime`` on a ``str``, a naive timestamp silently
    shifting the look-back window), this helper parses and validates the
    value up front so every connector receives the same well-formed UTC
    datetime.

    Rules:

    - ``None``/empty/whitespace → *default* (or the caller's documented
      fallback — e.g. the 90-day backfill window).
    - :class:`datetime` → returned as-is; naive datetimes are
      interpreted as UTC (the connector contract already treats all
      timestamps as UTC).
    - ``str`` → parsed with :func:`datetime.fromisoformat` (accepts
      truncated ISO forms such as ``2026-05-29`` and ``...T13:04:07``);
      a trailing ``Z`` and offsets are handled; naive results are
      interpreted as UTC.
    - Anything else, or an unparseable string → :class:`InvalidSinceError`
      with a *reason* that does not echo the raw value.

    Args:
        value: The raw ``since`` parameter.
        default: Fallback datetime when *value* is missing/empty.  When
            omitted, the caller is expected to supply its own default
            (the orchestrator's 90-day window); ``None`` is returned as a
            sentinel only when *default* is explicitly ``None``.

    Returns:
        An aware UTC :class:`datetime` (or *default* when *value* is
        missing and *default* was given).

    Raises:
        InvalidSinceError: When *value* cannot be interpreted as a
            datetime.  The message carries only the reason — never the
            raw input.
    """
    # Runtime guard: callers may pass untyped values (query params, MCP
    # input, stored state).  Cast to Any so the isinstance chain below is
    # a genuine runtime check, not a no-op narrowing of the declared type.
    raw: Any = value
    if raw is None:
        return default if default is not None else datetime.now(UTC)
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return default if default is not None else datetime.now(UTC)
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError:
            reason = "not a valid ISO-8601 datetime"
            raise InvalidSinceError(reason) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    reason = (
        f"unexpected type {type(raw).__name__}; expected datetime or ISO string"
    )
    raise InvalidSinceError(reason)
