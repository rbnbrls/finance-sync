"""Exception hierarchy for the market-intelligence source layer.

Every failure mode a provider can hit maps to a distinct exception so
the scheduler can classify runs (quota, auth, upstream outage) and
persist *sanitised* errors without leaking credentials.
"""

from __future__ import annotations


class IntelProviderError(Exception):
    """Base class for all market-intelligence provider errors."""


class IntelProviderAuthError(IntelProviderError):
    """Authentication/authorisation failed (bad or missing credentials)."""


class IntelProviderRateLimitError(IntelProviderError):
    """The provider's rate limit / quota was exceeded.

    Carries the ``Retry-After`` value (seconds) the provider sent, so
    retries can respect the window instead of hammering the source.
    """

    def __init__(
        self, message: str = "", *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class IntelProviderTimeoutError(IntelProviderError):
    """The provider did not answer within the configured timeout."""


class IntelProviderUnavailableError(IntelProviderError):
    """The provider is down / unreachable (upstream outage)."""


class IntelProviderInvalidResponseError(IntelProviderError):
    """The provider returned a malformed or unexpected payload."""


class IntelProviderConfigError(IntelProviderError):
    """The provider is misconfigured (missing URL, bad options)."""


class IntelLicensingError(IntelProviderError):
    """A licensing-policy violation was attempted (e.g. storing full
    text of a source whose license forbids it)."""


class IntelIngestionError(Exception):
    """Base class for ingestion-layer errors (dedupe, persistence)."""
