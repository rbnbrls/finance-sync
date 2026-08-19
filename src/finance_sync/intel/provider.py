"""Provider interface for the market-intelligence source layer.

Every provider adapter implements :class:`IntelProvider`:

* **Capability discovery** — :meth:`IntelProvider.capabilities` returns
  the kinds the provider can satisfy; :meth:`available` returns the
  runtime availability per capability (explicit ``unavailable``, never
  the absence of data).
* **Rate limits** — each provider declares its own
  :class:`IntelRateLimit` and enforces it through a shared sliding
  window (:class:`IntelRateLimiter`).
* **Retries** — transient failures (timeouts, 5xx, 429) are retried
  with exponential backoff via :meth:`IntelProvider.fetch_with_retry`.
* **Freshness** — :class:`IntelFreshnessPolicy` declares the natural
  refresh cadence of the source; the scheduler refreshes each provider
  according to its own policy.

Errors are raised as the typed hierarchy in
``finance_sync.intel.exceptions`` so the scheduler can classify runs
and persist *sanitised* errors (never credentials).
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from finance_sync.intel.enums import IntelAvailability, IntelCapability
from finance_sync.intel.exceptions import (
    IntelProviderError,
    IntelProviderRateLimitError,
    IntelProviderTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from finance_sync.intel.models import IntelItem

logger = logging.getLogger(__name__)


class IntelRateLimit(BaseModel):
    """Declared rate limit of a provider (requests per window)."""

    max_requests: int = 10
    window_seconds: int = 1
    respect: bool = True


class IntelRateLimiter:
    """Sliding-window rate limiter shared by provider adapters."""

    def __init__(self, policy: IntelRateLimit) -> None:
        self.policy = policy
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a request slot is available within the window."""
        if not self.policy.respect or self.policy.max_requests <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            cutoff = now - self.policy.window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self.policy.max_requests:
                oldest = self._timestamps[0]
                sleep_for = oldest + self.policy.window_seconds - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = asyncio.get_event_loop().time()
                self._timestamps = [
                    t
                    for t in self._timestamps
                    if t > now - self.policy.window_seconds
                ]
            self._timestamps.append(now)


class IntelFreshnessPolicy(BaseModel):
    """Natural refresh cadence of a provider source.

    ``max_age`` is the age beyond which stored data is considered stale
    (the scheduler should re-fetch).  ``min_interval`` is the earliest
    allowed re-fetch spacing (never hammer a source).
    """

    max_age: timedelta = timedelta(hours=6)
    min_interval: timedelta = timedelta(minutes=15)


class IntelProviderStatus(BaseModel):
    """Runtime availability snapshot of a provider."""

    provider: str = Field(description="Provider key")
    capabilities: list[IntelCapability] = Field(
        default_factory=list[IntelCapability],
        description="Capabilities this provider offers",
    )
    availability: dict[IntelCapability, IntelAvailability] = Field(
        default_factory=dict[IntelCapability, IntelAvailability],
        description="Runtime availability per capability",
    )
    last_run_at: datetime | None = Field(default=None)
    last_success_at: datetime | None = Field(default=None)
    last_error: str | None = Field(default=None)
    latency_ms: int | None = Field(default=None)
    rate_limit: IntelRateLimit = Field(default_factory=IntelRateLimit)


class IntelProvider(ABC):
    """Base class for market-intelligence provider adapters.

    Subclasses declare ``provider_key``, ``display_name``,
    ``capabilities()``, ``available(capability)``, a
    ``rate_limit`` policy, a ``freshness`` policy and a single
    ``fetch(capability, ...)`` entry point.
    """

    provider_key: str = ""
    display_name: str = ""
    license_note: str = ""
    config_url: str = ""

    def __init__(
        self,
        *,
        enabled: bool = True,
        rate_limit: IntelRateLimit | None = None,
        freshness: IntelFreshnessPolicy | None = None,
        retry_max_attempts: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.enabled = enabled
        self.retry_max_attempts = max(1, retry_max_attempts)
        self.retry_base_delay = max(0.1, retry_base_delay)
        self._rate_limiter = IntelRateLimiter(
            rate_limit or self.default_rate_limit()
        )
        self._freshness = freshness or self.default_freshness()

    # ── Subclass contract ───────────────────────────────────────────

    @abstractmethod
    async def capabilities(self) -> Sequence[IntelCapability]:
        """Return the capability kinds this provider can satisfy."""

    @abstractmethod
    async def available(self, capability: IntelCapability) -> IntelAvailability:
        """Return runtime availability for a capability.

        Must return an explicit ``unavailable`` (never raise) when the
        source cannot be reached or the capability is not configured.
        """

    @abstractmethod
    async def fetch(
        self,
        capability: IntelCapability,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
    ) -> Sequence[IntelItem]:
        """Fetch items for *capability*.

        Implementations MUST return items whose ``content_hash`` was
        computed with :func:`finance_sync.intel.hashing.content_hash`
        and whose storage flags honour the source license.
        """
        if TYPE_CHECKING:
            from finance_sync.intel.models import IntelItem as _ItemType

            return [_ItemType()]  # pragma: no cover — typing-only stub
        return None

    # ── Defaults ────────────────────────────────────────────────────

    @staticmethod
    def default_rate_limit() -> IntelRateLimit:
        """Declared default rate limit of the provider."""
        return IntelRateLimit(max_requests=10, window_seconds=1)

    @staticmethod
    def default_freshness() -> IntelFreshnessPolicy:
        """Declared default freshness policy of the provider."""
        return IntelFreshnessPolicy()

    @property
    def freshness(self) -> IntelFreshnessPolicy:
        """Freshness policy of this provider."""
        return self._freshness

    def configure(self, credentials: dict[str, str]) -> None:
        """Inject decrypted provider credentials before a run.

        Called by the scheduler with the tenant's envelope-decrypted
        credentials (never plaintext at rest, never logged).  The
        default implementation ignores the credentials — adapters that
        need a key (e.g. OpenBB) override this.  Must be safe to call
        with an empty dict (no-op) and idempotent.
        """
        del credentials  # default: no credentials required

    # ── Shared machinery ────────────────────────────────────────────

    async def fetch_page(
        self,
        capability: IntelCapability,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
        cursor: Any | None = None,
    ) -> tuple[Sequence[IntelItem], Any | None]:
        """Fetch one page of items, returning ``(items, next_cursor)``.

        The default implementation returns everything in a single page
        (``next_cursor=None``) by delegating to :meth:`fetch`.  Adapters
        that support cursor-based paging override this so the scheduler
        can ingest page-by-page — a failure on page N never rolls back
        pages 1..N-1 (partial-success semantics).

        *cursor* is accepted for interface uniformity; the default
        single-page implementation has no pages to advance.
        """
        del cursor
        items = await self.fetch(
            capability,
            identifiers=identifiers,
            limit=limit,
        )
        return items, None

    async def fetch_with_retry(
        self,
        capability: IntelCapability,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
        page: Any | None = None,
    ) -> tuple[Sequence[IntelItem], Any | None]:
        """Fetch one page with rate limiting, retries and backoff.

        Returns ``(items, next_cursor)``.  Transient failures
        (429/5xx/timeouts) are retried up to ``retry_max_attempts`` with
        exponential backoff plus jitter; a 429's ``Retry-After`` window
        is always respected (no request before it expires).  Non-transient
        provider errors propagate immediately.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                await self._rate_limiter.acquire()
                return await self.fetch_page(
                    capability,
                    identifiers=identifiers,
                    limit=limit,
                    cursor=page,
                )
            except IntelProviderRateLimitError as exc:
                last_exc = exc
                if attempt >= self.retry_max_attempts:
                    break
                # Respect the provider's Retry-After window — never
                # call again before it expires (thundering-herd guard).
                await self._backoff(attempt, retry_after=exc.retry_after)
            except IntelProviderTimeoutError as exc:
                last_exc = exc
                if attempt >= self.retry_max_attempts:
                    break
                await self._backoff(attempt)
            except IntelProviderError:
                # Non-transient provider errors propagate immediately
                raise
            except Exception as exc:
                last_exc = exc
                if attempt >= self.retry_max_attempts:
                    break
                await self._backoff(attempt)
        if last_exc is not None:
            raise last_exc
        return [], None

    async def _backoff(
        self, attempt: int, *, retry_after: float | None = None
    ) -> None:
        """Sleep with exponential backoff plus jitter.

        When the provider sent a ``Retry-After`` header, the sleep is
        at least that long (no request is issued before the window
        expires).
        """
        delay = self.retry_base_delay * (2 ** (attempt - 1))
        if retry_after is not None and retry_after > 0:
            delay = max(delay, retry_after)
        jitter = random.uniform(0, 0.25 * delay)
        await asyncio.sleep(delay + jitter)

    async def status(self) -> IntelProviderStatus:
        """Return a provider status snapshot (capabilities + availability)."""
        caps: Sequence[IntelCapability] = await self.capabilities()
        availability: dict[IntelCapability, IntelAvailability] = {}
        for cap in caps:
            availability[cap] = await self.available(cap)
        return IntelProviderStatus(
            provider=self.provider_key,
            capabilities=list(caps),
            availability=availability,
            rate_limit=self._rate_limiter.policy,
        )
