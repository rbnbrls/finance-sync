"""Release 16 connector retry and rate-limit chaos contracts."""

# pyright: basic

import asyncio
import json
from pathlib import Path

import pytest

from finance_sync.connectors.exceptions import PermanentError, RateLimitError
from finance_sync.connectors.rate_limiter import RateLimiter, RateLimitPolicy
from finance_sync.utils.redaction import sanitize_error


@pytest.mark.parametrize("provider", ["bank-fixture", "broker-fixture"])
async def test_transient_chaos_is_bounded_and_idempotent(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    events: set[str] = set()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fetch() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            message = f"{provider} fixture is rate limited"
            raise RateLimitError(message, retry_after=1)
        event_id = f"{provider}-event-1"
        events.add(event_id)
        return event_id

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    limiter = RateLimiter(RateLimitPolicy(backoff_base=1, jitter=0, max_retries=3))
    result = await limiter.retry(fetch)
    assert result == f"{provider}-event-1"
    assert attempts == 3
    assert delays == [1, 2]
    assert events == {f"{provider}-event-1"}


async def test_malformed_response_is_permanent_and_redacted() -> None:
    attempts = 0

    async def fetch() -> None:
        nonlocal attempts
        attempts += 1
        message = "malformed provider response token=api_secret_123456789"
        raise PermanentError(message)

    limiter = RateLimiter(RateLimitPolicy(max_retries=3))
    with pytest.raises(PermanentError) as error:
        await limiter.retry(fetch)
    assert attempts == 1
    safe = sanitize_error(str(error.value))
    assert "api_secret_123456789" not in safe
    assert "provider response" in safe


def test_chaos_matrix_is_synthetic_and_covers_bank_and_broker() -> None:
    matrix = json.loads(Path("config/connector-chaos-scenarios.json").read_text())
    assert matrix["synthetic_data_only"] is True
    assert set(matrix["providers"]) == {"bank-fixture", "broker-fixture"}
    assert {item["name"] for item in matrix["scenarios"]} >= {
        "timeout", "http_429", "malformed_response", "redis_transient_failure", "database_transient_failure"
    }
    assert matrix["credentials_logged"] is False
    assert matrix["provider_payloads_logged"] is False


def test_ci_runs_connector_chaos_matrix() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "connector-chaos:" in workflow
    assert "test_release16_connector_chaos.py" in workflow
