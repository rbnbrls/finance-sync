"""Connector and exporter error hierarchy.

All connector errors inherit from ``ConnectorError``.
All exporter errors inherit from ``ExporterError``.

- ``TransientError`` — safe to retry (network blips, rate limits, timeouts).
- ``PermanentError`` — must not be retried (bad auth, invalid data, resource not found).
- ``RateLimitError`` — a specialised transient error that carries a ``retry_after`` hint.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base exception for all connector-level errors."""


class TransientError(ConnectorError):
    """A temporary failure that is safe to retry.

    Examples: network timeouts, HTTP 503, temporary provider outages.
    """


class PermanentError(ConnectorError):
    """A non-recoverable failure that **must not** be retried.

    Examples: invalid credentials, malformed provider response, resource
    not found at the provider.
    """


class RateLimitError(TransientError):
    """The provider returned a rate-limit response (HTTP 429 or equivalent).

    Attributes:
        retry_after:  Recommended wait time in seconds, if the provider
            communicated one.  ``None`` if unknown.
    """

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        hint = f" (retry after {retry_after}s)" if retry_after else ""
        super().__init__(f"{message}{hint}")


class ExporterError(Exception):
    """Base exception for all exporter-level errors."""


class ExporterTransientError(ExporterError):
    """A temporary exporter failure that is safe to retry."""


class ExporterPermanentError(ExporterError):
    """A non-recoverable exporter failure that must not be retried."""
