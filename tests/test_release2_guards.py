"""Focused regression tests for release 2 resource and network guards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from finance_sync.config.settings import Settings
from finance_sync.services.webhook import WebhookService, validate_webhook_url


def test_webhook_policy_requires_public_https() -> None:
    validate_webhook_url("https://example.com/hook")
    with pytest.raises(ValueError, match=r"HTTPS|loopback"):
        validate_webhook_url("http://example.com/hook")
    with pytest.raises(ValueError, match="Private"):
        validate_webhook_url("https://127.0.0.1/hook")
    # Loopback HTTP is allowed (local/self-hosted receivers, E2E harness).
    validate_webhook_url("http://127.0.0.1:8080/hook")
    validate_webhook_url("http://localhost:8080/hook")
    validate_webhook_url("http://[::1]:8080/hook")
    # Private LAN HTTP stays blocked (SSRF protection).
    with pytest.raises(ValueError, match="loopback"):
        validate_webhook_url("http://192.168.1.8:8080/hook")
    with pytest.raises(ValueError, match="Private"):
        validate_webhook_url("https://10.0.0.5/hook")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True


@pytest.mark.asyncio
async def test_webhook_rate_limit_uses_shared_redis() -> None:
    redis = _FakeRedis()
    settings = Settings(_env_file=None)
    service = WebhookService(object(), settings, redis_client=redis)  # type: ignore[arg-type]
    webhook = SimpleNamespace(id="hook-1", rate_limit_max_per_minute=1)

    assert await service._is_rate_allowed(webhook) is True  # type: ignore[arg-type]
    assert await service._is_rate_allowed(webhook) is False  # type: ignore[arg-type]
    assert redis.expirations


def test_settings_include_upload_batch_limit() -> None:
    settings = Settings(_env_file=None)
    assert settings.degiro_import_max_batch_bytes >= (
        settings.degiro_import_max_file_bytes
    )
