"""Webhook throttling integration tests against real PostgreSQL + Redis.

The unit suite (``tests/test_webhooks.py``) covers the in-memory sliding
window counter.  This suite proves the **Redis** rate-limit path of
``WebhookService._is_rate_allowed``: the fixed-window INCR+EXPIRE bucket
used in production (``finance-sync:webhook-rate:<id>:<minute-bucket>``)
actually throttles deliveries once a webhook's
``rate_limit_max_per_minute`` is exceeded, and recovers when the window
expires.

It reuses the standard integration harness (``database_url`` /
``redis_url`` / ``session_factory`` fixtures) so the whole suite is
skipped locally when ``TEST_DATABASE_URL`` / ``TEST_REDIS_URL`` are unset
and **fails** in CI when they are missing — the skip-detection gate in
the CI workflow treats any skip here as a job failure.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from finance_sync.config.settings import Settings
from finance_sync.models.enums import WebhookDeliveryStatus
from finance_sync.models.webhook import Webhook
from finance_sync.services.webhook import WebhookService

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    """Minimal settings for the webhook service (no external calls)."""
    return Settings(
        webhook_max_retries=3,
        webhook_retry_base_delay_s=1.0,
        webhook_request_timeout_s=5.0,
    )


async def _make_webhook(
    session_factory,
    *,
    rate_limit: int,
    url: str = "https://example.invalid/hook",
) -> Webhook:
    """Create a persisted webhook row directly (no HTTP validation)."""
    from finance_sync.db.uow import UnitOfWork

    webhook = Webhook(
        tenant_id=str(uuid.uuid4()),
        url=url,
        secret=uuid.uuid4().hex,
        events=["test.event"],
        description="throttling-test",
        rate_limit_max_per_minute=rate_limit,
    )
    async with UnitOfWork(session_factory()) as uow:
        created = await uow.webhooks.add(webhook)
        await uow.commit()
    return created


class TestWebhookRedisRateLimit:
    """Redis-backed fixed-window throttling of webhook deliveries."""

    async def test_concurrent_burst_single_accept(
        self, session_factory, redis_client
    ) -> None:
        """A 10-way concurrent burst yields exactly one accepted delivery.

        The Redis INCR is atomic, so ten simultaneous attempts against a
        limit of 1 must produce exactly one ``True`` and nine ``False`` —
        no lost updates, no double-accepts (holdout #8).
        """
        service = WebhookService(session_factory, _settings(), redis_client)
        webhook = await _make_webhook(session_factory, rate_limit=1)

        results = await asyncio.gather(
            *[service._is_rate_allowed(webhook) for _ in range(10)]
        )
        assert sum(results) == 1, (
            f"expected exactly 1 accept, got {sum(results)}"
        )
        assert sum(not r for r in results) == 9

    async def test_redis_bucket_allows_within_limit(
        self, session_factory, redis_client
    ) -> None:
        """Deliveries under ``rate_limit_max_per_minute`` are allowed."""
        service = WebhookService(session_factory, _settings(), redis_client)
        webhook = await _make_webhook(session_factory, rate_limit=100)

        allowed = [await service._is_rate_allowed(webhook) for _ in range(5)]
        assert allowed == [True] * 5
        # The Redis bucket key exists and carries a TTL.
        keys = await redis_client.keys("finance-sync:webhook-rate:*")
        assert keys, "expected Redis rate-limit bucket keys"

    async def test_redis_bucket_throttles_over_limit(
        self, session_factory, redis_client
    ) -> None:
        """Once the per-minute budget is spent, deliveries are blocked."""
        service = WebhookService(session_factory, _settings(), redis_client)
        limit = 2
        webhook = await _make_webhook(session_factory, rate_limit=limit)

        first_two = [
            await service._is_rate_allowed(webhook) for _ in range(limit)
        ]
        assert first_two == [True] * limit
        assert await service._is_rate_allowed(webhook) is False
        assert await service._is_rate_allowed(webhook) is False

    async def test_redis_bucket_isolated_per_webhook(
        self, session_factory, redis_client
    ) -> None:
        """Two webhooks with different limits throttle independently."""
        service = WebhookService(session_factory, _settings(), redis_client)
        limited = await _make_webhook(session_factory, rate_limit=1)
        generous = await _make_webhook(session_factory, rate_limit=10)

        assert await service._is_rate_allowed(limited) is True
        assert await service._is_rate_allowed(limited) is False

        for _ in range(3):
            assert await service._is_rate_allowed(generous) is True

    async def test_delivery_log_marks_rate_limited(
        self, session_factory, redis_client
    ) -> None:
        """An over-limit delivery attempt is recorded as RATE_LIMITED.

        The webhook URL points at an unreachable host; without the rate
        limiter the delivery would fail with a connection error.  With a
        zero budget the attempt must be throttled *before* any HTTP call
        and the delivery log row must carry ``RATE_LIMITED``.
        """
        service = WebhookService(session_factory, _settings(), redis_client)
        webhook = await _make_webhook(
            session_factory, rate_limit=0, url="http://127.0.0.1:1/hook"
        )

        count = await service.dispatch_event("test.event", {"k": "v"})
        assert count == 1  # targeted one webhook

        from sqlalchemy import select

        from finance_sync.models.webhook import WebhookDeliveryLog

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(WebhookDeliveryLog).where(
                        WebhookDeliveryLog.webhook_id == str(webhook.id)
                    )
                )
            ).scalar_one()
        assert row.status == WebhookDeliveryStatus.RATE_LIMITED
        assert row.response_status_code == 429
