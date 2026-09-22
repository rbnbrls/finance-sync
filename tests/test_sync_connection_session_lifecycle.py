"""Regression tests for connection sync database session ownership."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.api.v1.sync import _run_connection_sync
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.results import SyncResult


@pytest.mark.asyncio
async def test_connection_sync_releases_request_session_before_provider_io() -> (
    None
):
    """Provider authentication must not hold the caller's DB connection."""
    db = MagicMock()
    db.close = AsyncMock()
    audit_db = MagicMock()
    audit_context = MagicMock()
    audit_context.__aenter__ = AsyncMock(return_value=audit_db)
    audit_context.__aexit__ = AsyncMock(return_value=None)
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=MagicMock(return_value=audit_context),
    )
    credential = SimpleNamespace(
        id="connection-1",
        tenant_id="tenant-1",
        provider_key="trading212",
        status="active",
        selected_accounts=[],
    )
    result = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        accounts_synced=1,
        transactions_synced=1,
        holdings_synced=0,
        unresolved_securities=0,
        error_message=None,
        error_category=None,
    )
    orchestrator = MagicMock()

    async def assert_session_released(
        *_args: object, **_kwargs: object
    ) -> SimpleNamespace:
        db.close.assert_awaited_once()
        return result

    orchestrator.run_sync = AsyncMock(side_effect=assert_session_released)
    latest_run_id = AsyncMock(return_value="run-1")
    record_audit = AsyncMock()

    with (
        patch(
            "finance_sync.api.v1.sync._decrypt_config",
            return_value=SimpleNamespace(
                connection_id="connection-1",
                selected_accounts=[],
            ),
        ),
        patch(
            "finance_sync.api.v1.sync.SyncOrchestrator",
            return_value=orchestrator,
        ),
        patch(
            "finance_sync.api.v1.sync._latest_run_id",
            new=latest_run_id,
        ),
        patch(
            "finance_sync.api.v1.sync._record_sync_audit",
            new=record_audit,
        ),
    ):
        link = await _run_connection_sync(
            container,
            db,
            "tenant-1",
            credential,
        )

    assert link.status == "completed"
    db.close.assert_awaited_once()
    assert latest_run_id.await_args.args[0] is audit_db
    assert record_audit.await_args.args[0] is audit_db
    assert latest_run_id.await_args.args[0] is not db
    assert record_audit.await_args.args[0] is not db


@pytest.mark.asyncio
async def test_sync_authenticates_before_acquiring_pipeline_session() -> None:
    """Provider I/O must not begin while the pipeline session owns a connection."""
    events: list[str] = []
    connector = MagicMock()

    async def authenticate() -> None:
        events.append("authenticate")

    connector.authenticate = AsyncMock(side_effect=authenticate)
    registry = MagicMock()
    registry.get_connector.return_value = connector
    registry.list_connectors.return_value = {}
    session = MagicMock()
    session_context = MagicMock()

    async def enter_session() -> MagicMock:
        assert events == ["authenticate"]
        events.append("session")
        return session

    session_context.__aenter__ = AsyncMock(side_effect=enter_session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=session_context)
    orchestrator = SyncOrchestrator(
        session_factory=session_factory,
        registry=registry,
        tenant_id="tenant-1",
    )
    orchestrator._run_pipeline = AsyncMock(
        return_value=SyncResult(
            status=SyncRunStatus.COMPLETED,
            accounts_synced=0,
            transactions_synced=0,
            holdings_synced=0,
            unresolved_securities=0,
            error_message=None,
            duration_s=0.0,
        )
    )

    await orchestrator.run_sync(
        "trading212",
        ConnectorConfig(
            provider_type="trading212",
            credentials={"api_key": "test"},
        ),
    )

    assert events == ["authenticate", "session"]
