"""Tests for the canonical three-level provider-health projection."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from finance_sync.services.provider_health import ProviderHealthService


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, *responses: _Result) -> None:
        self._responses = list(responses)

    async def execute(self, _statement: Any) -> _Result:
        return self._responses.pop(0)


def _credential(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": "connection-1",
        "tenant_id": "tenant-1",
        "provider_key": "demo",
        "status": "active",
        "encrypted_payload": b"encrypted",
        "last_error": None,
        "last_error_category": None,
        "last_test_status": "success",
        "last_test_at": datetime(2026, 8, 26, 9, tzinfo=UTC),
        "created_at": datetime(2026, 8, 25, tzinfo=UTC),
        "rate_limited_at": None,
        "retry_after_at": None,
        "rate_limit_attempts": 0,
        "rate_limit_scope": None,
        "last_http_status": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _run(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": "run-1",
        "connection_id": "connection-1",
        "status": "completed",
        "started_at": datetime(2026, 8, 26, 8, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 26, 8, 30, tzinfo=UTC),
        "items_processed": 12,
        "error_category": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def contracts(monkeypatch):
    lifecycle = {
        "connectors": [
            {
                "name": "demo",
                "version": "1.0.0",
                "previous_version": "0.9.0",
                "capabilities": ["accounts", "transactions"],
                "minimum_fixture_version": "2026-01-01",
                "certification_status": "certified",
                "certified_at": "2026-08-01",
                "certification_commit": "test",
                "removal_date": "2027-01-01",
            }
        ]
    }
    monkeypatch.setattr(
        "finance_sync.services.provider_health.load_json",
        lambda path: lifecycle,
    )
    monkeypatch.setattr(
        "finance_sync.services.provider_health.default_contract_paths",
        lambda: ("lifecycle", "matrix"),
    )


def _service(session: _Session, now: datetime) -> ProviderHealthService:
    registry = SimpleNamespace(
        list_connectors=lambda: {
            "demo": {
                "provider_key": "demo",
                "plugin_version": "1.0.0",
                "supported_resources": ["accounts", "transactions"],
            }
        }
    )
    return ProviderHealthService(
        session,
        "tenant-1",
        registry=registry,
        now=now,
    )


@pytest.mark.asyncio
async def test_credentials_without_successful_processing_need_sync(contracts) -> None:
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    result = await _service(
        _Session(_Result([_credential()]), _Result([])), now
    ).get_overview()

    assert len(result) == 1
    overview = result[0]
    assert overview.overall_status == "attention_required"
    assert overview.action_required == "run_sync"
    assert overview.connection.status == "connected"
    assert all(item.source_status == "not_processed" for item in overview.resources)
    assert overview.last_successful_processing.last_success_at is None


@pytest.mark.asyncio
async def test_fresh_successful_processing_is_healthy(contracts) -> None:
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    result = await _service(
        _Session(_Result([_credential()]), _Result([_run()])), now
    ).get_overview()

    overview = result[0]
    assert overview.overall_status == "healthy"
    assert overview.action_required is None
    assert overview.last_successful_processing.resources_processed == 2
    assert all(item.stale is False for item in overview.resources)


@pytest.mark.asyncio
async def test_failed_latest_run_is_error_but_history_is_preserved(contracts) -> None:
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    result = await _service(
        _Session(
            _Result([_credential()]),
            _Result(
                [
                    _run(
                        id="run-failed",
                        status="failed",
                        completed_at=None,
                        error_category="provider_unavailable",
                    ),
                    _run(),
                ]
            ),
        ),
        now,
    ).get_overview()

    overview = result[0]
    assert overview.overall_status == "error"
    assert overview.action_required == "run_sync"
    assert all(item.source_status == "failed" for item in overview.resources)
    assert all(item.last_success_at is not None for item in overview.resources)


def test_stale_resource_precedence_is_attention_required() -> None:
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    service = ProviderHealthService(None, "tenant-1", now=now)
    resources = [
        service._resource_health(
            "transactions",
            [_run(completed_at=now - timedelta(days=2))],
        )
    ]
    processing = service._processing_health(resources)

    status, action = service._overall_status(
        _credential(),
        service._connection(_credential()),
        resources,
        processing,
        "compatible",
    )

    assert (status, action) == ("attention_required", "run_sync")


def test_resource_scoped_failure_does_not_hide_healthy_sibling() -> None:
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    service = ProviderHealthService(None, "tenant-1", now=now)
    runs = [
        _run(
            id="accounts-failed",
            status="failed",
            completed_at=None,
            error_category="provider_unavailable",
            resource="accounts",
        ),
        _run(resource="transactions"),
    ]

    accounts = service._resource_health("accounts", runs)
    transactions = service._resource_health("transactions", runs)

    assert accounts.source_status == "failed"
    assert transactions.source_status == "healthy"


def test_rate_limit_diagnosis_is_safe_and_actionable() -> None:
    now = datetime.now(UTC)
    retry_at = now + timedelta(minutes=5)
    credential = _credential(
        rate_limited_at=now,
        retry_after_at=retry_at,
        rate_limit_attempts=3,
        rate_limit_scope="connection",
        last_http_status=429,
        last_error="provider rate limit (secret-redacted)",
        last_error_category="rate_limited",
    )

    health = ProviderHealthService._connection(credential)

    assert health.rate_limit.active is True
    assert health.rate_limit.retry_after_at == retry_at
    assert health.rate_limit.attempt_count == 3
    assert health.rate_limit.last_http_status == 429
    assert "secret" not in health.rate_limit.model_dump_json()


def test_expired_credential_requires_reauthentication() -> None:
    credential = _credential(
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        credential_status="verified",
    )
    health = ProviderHealthService._connection(credential)
    assert health.status == "reauth_required"
    assert health.credential_status == "reauth_required"
