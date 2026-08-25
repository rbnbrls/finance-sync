"""Small Redis-backed lease used to make recovery actions single-flight."""

from __future__ import annotations

import secrets
from typing import Any


class RetryLease:
    """An expiring, owner-checked Redis lease."""

    def __init__(self, redis: Any, key: str, *, ttl_seconds: int = 900) -> None:
        self._redis = redis
        self.key = key
        self._token = secrets.token_urlsafe(18)
        self._ttl = ttl_seconds
        self.acquired = False

    async def __aenter__(self) -> RetryLease:
        self.acquired = bool(
            await self._redis.set(self.key, self._token, nx=True, ex=self._ttl)
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if not self.acquired:
            return
        await self._redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1,
            self.key,
            self._token,
        )


def retry_lease(
    redis: Any, *, tenant_id: str, kind: str, item_id: str
) -> RetryLease:
    """Build a tenant-scoped lease key for a sync or export retry."""
    return RetryLease(
        redis,
        f"finance-sync:recovery:{kind}:{tenant_id}:{item_id}",
    )
