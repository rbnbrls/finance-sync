"""Redis integration tests against a real Redis server.

Covers the primitives finance-sync relies on Redis for (per
ARCHITECTURE.md: "Redis is disposable cache, coordination, and
rate-limit state"):

* connectivity via the app's own ``Container.redis_client``
* distributed lock (SET NX EX + Lua compare-and-delete release)
* fixed-window rate-limit counter (INCR + EXPIRE)
* TTL-driven cache expiry
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.integration

RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class TestRedisConnectivity:
    """The app's Redis wiring works against a real server."""

    async def test_container_redis_client_ping(self, redis_url: str) -> None:
        """Container.from_settings builds a working client (health path)."""
        from finance_sync.config.settings import Settings
        from finance_sync.container import Container

        settings = Settings(redis_url=redis_url)  # type: ignore[call-arg]
        container = Container.from_settings(settings)
        client = container.redis_client
        try:
            result = await asyncio.wait_for(client.ping(), timeout=5)  # type: ignore[attr-defined]
            assert result is True
        finally:
            await client.aclose()  # type: ignore[attr-defined]

    async def test_set_get_roundtrip(self, redis_client) -> None:
        key = f"it:roundtrip:{uuid.uuid4().hex}"
        await redis_client.set(key, "hello")
        assert await redis_client.get(key) == "hello"
        await redis_client.delete(key)

    async def test_decode_responses_enabled(self, redis_client) -> None:
        """Container uses decode_responses=True; fixture mirrors that."""
        key = f"it:decode:{uuid.uuid4().hex}"
        await redis_client.set(key, "str-value")
        value = await redis_client.get(key)
        assert isinstance(value, str)


class TestDistributedLock:
    """SET NX EX lock with Lua compare-and-delete release."""

    async def test_concurrent_acquire_single_winner(self, redis_client) -> None:
        """Ten parallel acquirers — exactly one wins the lock.

        SET NX is atomic on Redis, so a concurrent burst must yield a
        single winner (holdout #8: concurrency semantics on real Redis).
        """
        key = f"it:lock:race:{uuid.uuid4().hex}"

        async def _try_acquire() -> bool:
            result = await redis_client.set(key, "t", nx=True, ex=60)
            return result is True

        outcomes = await asyncio.gather(*[_try_acquire() for _ in range(10)])
        assert sum(outcomes) == 1, f"expected 1 winner, got {sum(outcomes)}"

        # Winner's token is stored; the lock is held.
        assert await redis_client.get(key) == "t"

    async def test_acquire_and_release(self, redis_client) -> None:
        key = f"it:lock:{uuid.uuid4().hex}"
        token = uuid.uuid4().hex

        acquired = await redis_client.set(key, token, nx=True, ex=60)
        assert acquired is True

        # A second client cannot acquire while held
        other = await redis_client.set(key, "other-token", nx=True, ex=60)
        assert other is None

        # Owner releases via Lua compare-and-delete
        released = await redis_client.eval(RELEASE_LUA, 1, key, token)
        assert released == 1
        assert await redis_client.get(key) is None

    async def test_release_requires_owner_token(self, redis_client) -> None:
        key = f"it:lock:{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        await redis_client.set(key, token, nx=True, ex=60)

        # Wrong token cannot release
        released = await redis_client.eval(RELEASE_LUA, 1, key, "wrong-token")
        assert released == 0
        assert await redis_client.get(key) == token

    async def test_lock_expires_after_ttl(self, redis_client) -> None:
        key = f"it:lock:{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        await redis_client.set(key, token, nx=True, ex=1)

        # Lock is held...
        other = await redis_client.set(key, "b", nx=True, ex=60)
        assert other is None

        # ...and auto-released after TTL (no stale lock forever)
        await asyncio.sleep(1.2)
        acquired = await redis_client.set(key, "b", nx=True, ex=60)
        assert acquired is True


class TestRedisRateLimit:
    """Fixed-window rate limiting via INCR + EXPIRE."""

    async def test_fixed_window_counter(self, redis_client) -> None:
        key = f"it:rl:{uuid.uuid4().hex}"
        limit = 3

        for i in range(limit):
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, 60)
            assert count == i + 1

        # Over the limit — counter keeps counting, window not yet reset
        over = await redis_client.incr(key)
        assert over == limit + 1

        ttl = await redis_client.ttl(key)
        assert ttl > 0

    async def test_counter_expires_and_resets(self, redis_client) -> None:
        key = f"it:rl:{uuid.uuid4().hex}"
        await redis_client.incr(key)
        await redis_client.expire(key, 1)

        await asyncio.sleep(1.2)
        assert await redis_client.get(key) is None
        # Window reset → first request is count 1 again
        count = await redis_client.incr(key)
        assert count == 1


class TestRedisCache:
    """TTL-driven cache semantics (price cache, FX cache)."""

    async def test_ttl_expiry(self, redis_client) -> None:
        key = f"it:cache:{uuid.uuid4().hex}"
        await redis_client.set(key, "v", ex=1)
        assert await redis_client.get(key) == "v"
        await asyncio.sleep(1.2)
        assert await redis_client.get(key) is None

    async def test_getset_pattern(self, redis_client) -> None:
        """Cache-aside: fetch, miss, populate with TTL."""
        key = f"it:cache:{uuid.uuid4().hex}"
        assert await redis_client.get(key) is None

        value = "computed"
        await redis_client.set(key, value, ex=60)
        assert await redis_client.get(key) == value
