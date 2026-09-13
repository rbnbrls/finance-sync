"""Shared Redis quota reservation for remediation workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    requests: int
    window_seconds: int
    endpoint_family: str
    concurrency: int | None = None


@dataclass(frozen=True, slots=True)
class Reservation:
    allowed: bool
    retry_at: datetime | None = None
    reason: str | None = None


def parse_retry_after(
    value: str | int | float | None,
    *,
    now: datetime | None = None,
    cap_seconds: int = 3600,
) -> datetime | None:
    """Parse seconds or an HTTP-date, bounded for safe scheduling."""
    if value is None:
        return None
    current = now or datetime.now(UTC)
    try:
        delay = max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            delay = max(0.0, (parsed.astimezone(UTC) - current).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return current + timedelta(seconds=min(delay, cap_seconds))


class RemediationRateLimitCoordinator:
    """Atomic fixed-window reservation shared by every worker process."""

    _SCRIPT = """
    if redis.call('exists', KEYS[2]) == 1 then
      return {0, redis.call('ttl', KEYS[2]), 2}
    end
    local count = tonumber(redis.call('get', KEYS[1]) or '0')
    if count + tonumber(ARGV[1]) > tonumber(ARGV[2]) then
      return {0, redis.call('ttl', KEYS[1]), 1}
    end
    redis.call('incrby', KEYS[1], ARGV[1])
    redis.call('expire', KEYS[1], ARGV[3])
    return {1, 0, 0}
    """

    def __init__(
        self, redis: Any, *, prefix: str = "finance-sync:dq-quota"
    ) -> None:
        self.redis = redis
        self.prefix = prefix

    def _key(self, provider: str, scope: str, endpoint_family: str) -> str:
        return f"{self.prefix}:{provider}:{scope}:{endpoint_family}"

    async def reserve(
        self, provider: str, scope: str, policy: QuotaPolicy, *, cost: int = 1
    ) -> Reservation:
        if self.redis is None:
            return Reservation(False, reason="redis_not_configured")
        key = self._key(provider, scope, policy.endpoint_family)
        cooldown_key = key + ":cooldown"
        result = await self.redis.eval(
            self._SCRIPT,
            2,
            key,
            cooldown_key,
            cost,
            policy.requests,
            policy.window_seconds,
        )
        allowed, ttl = int(result[0]), int(result[1])
        if allowed:
            return Reservation(True)
        reason = (
            "cooldown"
            if len(result) > 2 and int(result[2]) == 2
            else "quota_exhausted"
        )
        return Reservation(
            False,
            datetime.now(UTC) + timedelta(seconds=max(ttl, 1)),
            reason,
        )

    async def cooldown(
        self, provider: str, scope: str, endpoint_family: str, until: datetime
    ) -> None:
        if self.redis is None:
            return
        seconds = max(1, int((until - datetime.now(UTC)).total_seconds()))
        await self.redis.set(
            self._key(provider, scope, endpoint_family) + ":cooldown",
            "1",
            ex=seconds,
        )

    async def is_cooled_down(
        self, provider: str, scope: str, endpoint_family: str
    ) -> bool:
        if self.redis is None:
            return False
        return bool(
            await self.redis.exists(
                self._key(provider, scope, endpoint_family) + ":cooldown"
            )
        )
