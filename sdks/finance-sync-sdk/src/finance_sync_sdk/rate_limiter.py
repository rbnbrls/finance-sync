"""Rate-limit handling with exponential backoff and jitter.

Connector plugins that subclass :class:`ConnectorPlugin` automatically get
rate-limited method wrappers when they attach a :class:`RateLimitPolicy`.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


@dataclass
class RateLimitPolicy:
    """Declarative rate-limit policy for a connector plugin.

    Usage in a plugin::

        rate_limit_policy = RateLimitPolicy(
            max_requests=100,
            window_seconds=60,
            backoff_base=1.0,
            max_retries=5,
        )
    """

    max_requests: int = 60
    """Maximum number of requests allowed within ``window_seconds``."""

    window_seconds: float = 60.0
    """Sliding window duration."""

    backoff_base: float = 1.0
    """Base delay (seconds) for exponential backoff on transient errors."""

    backoff_cap: float = 120.0
    """Maximum delay (seconds) a single backoff can reach."""

    max_retries: int = 5
    """Maximum number of retries for a single operation."""

    jitter: float = 0.1
    """Random jitter fraction — delay is multiplied by
    ``[1 - jitter, 1 + jitter]``."""

    #: Arbitrary metadata for documentation / UI.
    metadata: dict[str, Any] = field(default_factory=dict)


class RateLimiter:
    """Sliding-window rate limiter with exponential backoff.

    Uses a simple in-memory timestamp deque to track the request window.
    For multi-process deployments, wrap with a Redis-based implementation.
    """

    def __init__(self, policy: RateLimitPolicy | None = None) -> None:
        self.policy = policy or RateLimitPolicy()
        self._request_times: list[float] = []

    # ── Sliding-window throttle ────────────────────────────────────────

    async def acquire(self) -> None:
        """Wait until the slot window allows a new request.

        Blocks if ``max_requests`` have been made in the last
        ``window_seconds``.
        """
        if self.policy.max_requests <= 0:
            return

        now = _monotonic()
        cutoff = now - self.policy.window_seconds
        self._request_times = [t for t in self._request_times if t > cutoff]

        if len(self._request_times) >= self.policy.max_requests:
            sleep_for = (
                self._request_times[0] + self.policy.window_seconds - now
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._request_times = [
                t
                for t in self._request_times
                if t > _monotonic() - self.policy.window_seconds
            ]

        self._request_times.append(_monotonic())

    # ── Exponential backoff with jitter ────────────────────────────────

    def backoff_delay(self, attempt: int) -> float:
        """Return the delay in seconds for the given retry *attempt* (0-based).

        Uses exponential backoff with full jitter::

            delay = min(base * 2^attempt, cap) * uniform(1 - jitter, 1 + jitter)
        """
        base = self.policy.backoff_base * (2**attempt)
        capped = min(base, self.policy.backoff_cap)
        jitter_factor = 1.0 + random.uniform(
            -self.policy.jitter, self.policy.jitter
        )
        return capped * jitter_factor

    async def retry(
        self,
        coro_factory: Callable[[], Coroutine[None, None, object]],
    ) -> object:
        """Execute *coro_factory* with automatic retry on transient errors.

        Re-raises the last exception if all retries are exhausted or a
        permanent error is raised.
        """
        from finance_sync_sdk.exceptions import (
            PermanentError,
            TransientError,
        )

        last_exc: Exception | None = None

        for attempt in range(self.policy.max_retries + 1):
            try:
                await self.acquire()
                return await coro_factory()
            except TransientError as exc:
                last_exc = exc
                if attempt < self.policy.max_retries:
                    delay = self.backoff_delay(attempt)
                    await asyncio.sleep(delay)
            except PermanentError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.policy.max_retries:
                    delay = self.backoff_delay(attempt)
                    await asyncio.sleep(delay)

        msg = f"All {self.policy.max_retries} retries exhausted"
        raise TransientError(msg) from last_exc


def _monotonic() -> float:
    """Return monotonic time in seconds (overridable in tests)."""
    return asyncio.get_event_loop().time()
