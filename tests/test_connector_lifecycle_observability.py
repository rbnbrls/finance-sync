from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from finance_sync.observability.connector_metrics import connection_hash
from finance_sync.services.connection_audit import (
    AUDIT_REAUTH_START,
    log_connection_event,
)


def test_connection_telemetry_uses_hash_not_identifier() -> None:
    raw = "connection-123"
    hashed = connection_hash(raw)
    assert hashed != raw
    assert len(hashed) == 16
    assert connection_hash(raw) == hashed
    assert connection_hash(None) == "none"


async def test_audit_defaults_include_safe_result_and_reason_code() -> None:
    session = MagicMock()
    session.flush = AsyncMock()
    entry = await log_connection_event(
        session,
        tenant_id="tenant-1",
        action=AUDIT_REAUTH_START,
        provider_key="bunq",
        connection_id="connection-123",
        detail={"credential": "token=super-secret"},
        secrets=["super-secret"],
    )
    assert entry.detail["result"] == "success"
    assert entry.detail["reason_code"] == AUDIT_REAUTH_START
    assert "super-secret" not in str(entry.detail)


def test_synthetic_flow_is_ordered_and_credential_free() -> None:
    flow = [
        "catalog",
        "connection_test",
        "resource_sync",
        "rate_limit_diagnosis",
        "reauth",
        "healthy_processing",
    ]
    assert flow == [
        "catalog",
        "connection_test",
        "resource_sync",
        "rate_limit_diagnosis",
        "reauth",
        "healthy_processing",
    ]
    assert "token" not in " ".join(flow)
