"""Tests for the rate limiter."""
# pyright: basic

from __future__ import annotations

import pytest

from finance_sync.connectors.exceptions import PermanentError, TransientError
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy

pytestmark = pytest.mark.asyncio


class TestRateLimitPolicy:
    """RateLimitPolicy dataclass defaults and customisation."""

    def test_defaults(self) -> None:
        p = RateLimitPolicy()
        assert p.max_requests == 60
        assert p.window_seconds == 60.0
        assert p.backoff_base == 1.0
        assert p.max_retries == 5

    def test_custom(self) -> None:
        p = RateLimitPolicy(max_requests=10, max_retries=2, backoff_base=0.5)
        assert p.max_requests == 10
        assert p.max_retries == 2


class TestRateLimiterAcquire:
    """Sliding-window acquire logic."""

    async def test_no_throttle_when_under_limit(self) -> None:
        policy = RateLimitPolicy(max_requests=100, window_seconds=60)
        limiter = RateLimiter(policy)
        # Should complete without delay
        await limiter.acquire()
        await limiter.acquire()
        assert len(limiter._request_times) == 2

    async def test_disabled_when_max_requests_zero(self) -> None:
        policy = RateLimitPolicy(max_requests=0)
        limiter = RateLimiter(policy)
        await limiter.acquire()  # no-op
        assert limiter._request_times == []


class TestRateLimiterBackoff:
    """Exponential backoff delay calculation."""

    def test_backoff_increases(self) -> None:
        policy = RateLimitPolicy(backoff_base=1.0, jitter=0.0)
        limiter = RateLimiter(policy)
        d0 = limiter.backoff_delay(0)
        d1 = limiter.backoff_delay(1)
        d2 = limiter.backoff_delay(2)
        assert d0 == 1.0
        assert d1 == 2.0
        assert d2 == 4.0

    def test_backoff_capped(self) -> None:
        policy = RateLimitPolicy(backoff_base=1.0, backoff_cap=5.0, jitter=0.0)
        limiter = RateLimiter(policy)
        d3 = limiter.backoff_delay(3)  # 2^3 = 8, capped at 5
        assert d3 == 5.0

    def test_jitter_range(self) -> None:
        policy = RateLimitPolicy(backoff_base=10.0, jitter=0.5)
        limiter = RateLimiter(policy)
        delays = [limiter.backoff_delay(0) for _ in range(50)]
        # With jitter=0.5, each delay is 10 * uniform(0.5, 1.5)
        assert all(5.0 <= d <= 15.0 for d in delays)
        # Not all identical
        assert len({round(d, 2) for d in delays}) > 1


class TestRateLimiterRetry:
    """Automatic retry on transient errors."""

    async def test_success_on_first_attempt(self) -> None:
        policy = RateLimitPolicy(max_retries=3, backoff_base=0.01)
        limiter = RateLimiter(policy)

        call_count = 0

        async def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await limiter.retry(succeed)  # type: ignore[arg-type]
        assert result == "ok"
        assert call_count == 1

    async def test_retry_on_transient_then_succeed(self) -> None:
        policy = RateLimitPolicy(max_retries=3, backoff_base=0.01)
        limiter = RateLimiter(policy)

        call_count = 0

        async def fail_twice() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "not ready"
                raise TransientError(msg)
            return "ok"

        result = await limiter.retry(fail_twice)  # type: ignore[arg-type]
        assert result == "ok"
        assert call_count == 3

    async def test_exhaust_retries(self) -> None:
        policy = RateLimitPolicy(max_retries=2, backoff_base=0.01)
        limiter = RateLimiter(policy)

        call_count = 0

        async def always_fail() -> str:
            nonlocal call_count
            call_count += 1
            msg = "always fails"
            raise TransientError(msg)

        with pytest.raises(TransientError, match="retries exhausted"):
            await limiter.retry(always_fail)  # type: ignore[arg-type]
        assert call_count == 3  # initial + 2 retries

    async def test_does_not_retry_permanent_error(self) -> None:
        policy = RateLimitPolicy(max_retries=3, backoff_base=0.01)
        limiter = RateLimiter(policy)

        call_count = 0

        async def fail_permanent() -> str:
            nonlocal call_count
            call_count += 1
            msg = "bad auth"
            raise PermanentError(msg)

        with pytest.raises(PermanentError, match="bad auth"):
            await limiter.retry(fail_permanent)  # type: ignore[arg-type]
        assert call_count == 1  # no retry

    async def test_unknown_error_treated_as_transient(self) -> None:
        policy = RateLimitPolicy(max_retries=1, backoff_base=0.01)
        limiter = RateLimiter(policy)

        call_count = 0

        async def fail_unknown() -> str:
            nonlocal call_count
            call_count += 1
            msg = "unknown error"
            raise ValueError(msg)

        with pytest.raises(TransientError, match="retries exhausted"):
            await limiter.retry(fail_unknown)  # type: ignore[arg-type]
        assert call_count == 2  # initial + 1 retry
