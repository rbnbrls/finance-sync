"""Adversarial behavioral coverage for Phase 01's Wealthfolio bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from finance_sync.config import Settings
from finance_sync.exporter.wealthfolio.client import (
    WealthfolioClient,
    WealthfolioClientConfig,
    WealthfolioHealthError,
)
from finance_sync.reconciliation.remediation.wealthfolio import (
    WealthfolioQuoteStrategy,
)
from finance_sync.services.data_health import DataHealthService
from finance_sync.services.wealthfolio_health_bridge import (
    normalize_health_issues,
)

HEALTH_FIXTURE = Path(__file__).parent / "fixtures" / "wealthfolio_health_status.json"


def _response(status_code: int, payload: object = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.is_error = status_code >= 400
    response.json.return_value = payload
    return response


@pytest.mark.parametrize("transient_status", [408, 429, 500, 502, 503, 504])
async def test_health_poll_retries_transient_http_failures(
    transient_status: int,
) -> None:
    """A transient health failure is retried and a later valid payload wins."""
    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url="http://wealthfolio.test",
            password="do-not-leak-this-password",
            retry_408_base_delay=0,
        )
    )
    client._is_authenticated = True
    success_payload = {"issues": [], "newServerField": {"is": "tolerated"}}

    with (
        patch.object(
            client._client,
            "get",
            new=AsyncMock(
                side_effect=[_response(transient_status), _response(200, success_payload)]
            ),
        ) as get,
        patch("finance_sync.exporter.wealthfolio.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        assert await client.get_health_status() == success_payload

    assert get.await_count == 2
    assert sleep.await_count == 1
    await client.close()


async def test_health_poll_retries_timeout_then_returns_payload() -> None:
    """A transport timeout is retryable without exposing request secrets."""
    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url="http://wealthfolio.test",
            password="do-not-leak-this-password",
            retry_408_base_delay=0,
        )
    )
    client._is_authenticated = True
    get = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("socket timed out"),
            _response(200, {"issues": []}),
        ]
    )
    with (
        patch.object(client._client, "get", new=get),
        patch("finance_sync.exporter.wealthfolio.client.asyncio.sleep", new=AsyncMock()),
    ):
        assert await client.get_health_status() == {"issues": []}
    assert get.await_count == 2
    await client.close()


async def test_health_poll_retries_transient_request_error() -> None:
    """Connection resets and DNS failures use the same bounded retry path."""
    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url="http://wealthfolio.test",
            password="do-not-leak-this-password",
            retry_408_base_delay=0,
        )
    )
    client._is_authenticated = True
    request = httpx.Request("GET", "http://wealthfolio.test/api/v1/health/status")
    with (
        patch.object(
            client._client,
            "get",
            new=AsyncMock(
                side_effect=[
                    httpx.RequestError("connection reset", request=request),
                    _response(200, {"issues": []}),
                ]
            ),
        ) as get,
        patch("finance_sync.exporter.wealthfolio.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        assert await client.get_health_status() == {"issues": []}
    assert get.await_count == 2
    assert sleep.await_count == 1
    await client.close()


async def test_health_poll_exhaustion_is_bounded_and_redacted() -> None:
    """Exhausted rate limits become a classified error without body leakage."""
    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url="http://wealthfolio.test",
            password="do-not-leak-this-password",
            retry_408_base_delay=0,
        )
    )
    client._is_authenticated = True
    with (
        patch.object(
            client._client,
            "get",
            new=AsyncMock(return_value=_response(429, {"password": client._config.password})),
        ) as get,
        patch("finance_sync.exporter.wealthfolio.client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(WealthfolioHealthError, match="rate_limit") as exc_info,
    ):
        await client.get_health_status()

    assert exc_info.value.category == "rate_limit"
    assert get.await_count == client._config.retry_408_attempts
    assert client._config.password not in str(exc_info.value)
    await client.close()


def test_health_findings_are_independent_bounded_and_fail_closed() -> None:
    """Affected assets stay traceable while unsafe/unknown issues need review."""
    payload = json.loads(HEALTH_FIXTURE.read_text())
    payload["issues"][1]["details"] = "x" * 1000

    findings = normalize_health_issues(
        payload,
        tenant_id="tenant-a",
        target_id="target-wealthfolio-1",
    )

    assert len(findings) == 4
    assert [finding.affected_entity_id for finding in findings] == [
        "wf-asset-a",
        "wf-asset-b",
        "wf-asset-c",
        "wf-asset-d",
    ]
    assert findings[0].issue_type == "wealthfolio_historical_price_gap"
    assert findings[0].remediation_strategy == "wealthfolio_price_history"
    assert findings[0].context["security_id"] == "canonical-a"
    assert findings[1].context["security_id"] == "canonical-b"
    assert findings[2].issue_type == "wealthfolio_missing_purchase_price"
    assert findings[2].context["manual_review"] is True
    assert findings[3].issue_type == "wealthfolio_unsupported_issue"
    assert findings[3].context["manual_review"] is True
    assert len(findings[2].context["details"]) == 256
    assert "unknownTopLevelField" not in json.dumps(
        [finding.context for finding in findings]
    )


def test_bridge_is_disabled_by_default() -> None:
    """A fresh settings object cannot schedule health polling accidentally."""
    settings = Settings(_env_file=None)
    assert settings.wealthfolio_health_bridge_enabled is False
    assert settings.wealthfolio_health_bridge_interval_minutes == 15


@pytest.mark.asyncio
async def test_data_health_enabled_flag_is_separate_from_active_target() -> None:
    """An active target does not imply that the bridge feature is enabled."""
    cursor = SimpleNamespace(
        complete=False,
        truncated=True,
        last_error=None,
        last_successful_poll=None,
        issue_count=0,
    )

    class Result:
        def scalar_one_or_none(self) -> object:
            return cursor

    class Session:
        def __init__(self) -> None:
            self.scalars = iter([1, 0, 0])

        async def scalar(self, _statement: object) -> int:
            return next(self.scalars)

        async def execute(self, _statement: object) -> Result:
            return Result()

    bridge = await DataHealthService(
        Session(),
        "tenant-a",
        wealthfolio_health_bridge_enabled=False,
    )._wealthfolio_bridge_summary()

    assert bridge.enabled is False
    assert bridge.target_count == 1
    assert bridge.degraded is True


async def test_quote_repair_verification_requires_finance_sync_owned_quote() -> None:
    """Remote verification rejects a quote from an unrelated data source."""
    strategy = WealthfolioQuoteStrategy(MagicMock(), MagicMock())
    item = SimpleNamespace(context={"remote_entity_id": "wf-asset-a"})
    connector = MagicMock()
    connector.get_quote_history = AsyncMock(
        return_value=[{"timestamp": "2026-09-12", "dataSource": "OTHER"}]
    )
    connector.QUOTE_DATA_SOURCE = "CUSTOM_SCRAPER:finance-sync"

    result = await strategy.verify(item, connector)

    assert result.resolved is False
    assert "not verified" in result.reason
