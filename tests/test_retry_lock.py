"""Unit tests for recovery-action single-flight leases."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from finance_sync.services.retry_lock import RetryLease, retry_lease


@pytest.mark.asyncio
async def test_lease_acquires_with_expiry_and_releases_by_owner_token() -> None:
    redis = AsyncMock()
    redis.set.return_value = True
    lease = RetryLease(redis, "recovery:key", ttl_seconds=30)

    async with lease as entered:
        assert entered is lease
        assert lease.acquired is True

    redis.set.assert_awaited_once_with(
        "recovery:key", lease._token, nx=True, ex=30
    )
    redis.eval.assert_awaited_once_with(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1,
        "recovery:key",
        lease._token,
    )


@pytest.mark.asyncio
async def test_lease_conflict_does_not_release_another_owner() -> None:
    redis = AsyncMock()
    redis.set.return_value = None
    lease = RetryLease(redis, "recovery:key")

    async with lease:
        assert lease.acquired is False

    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_releases_when_body_raises() -> None:
    redis = AsyncMock()
    redis.set.return_value = True
    lease = RetryLease(redis, "recovery:key")
    error = "failed"

    with pytest.raises(RuntimeError, match=error):
        async with lease:
            raise RuntimeError(error)

    redis.eval.assert_awaited_once()


def test_retry_lease_key_is_tenant_and_action_scoped() -> None:
    redis = AsyncMock()

    lease = retry_lease(
        redis,
        tenant_id="tenant-a",
        kind="export",
        item_id="run-a",
    )

    assert lease.key == "finance-sync:recovery:export:tenant-a:run-a"
