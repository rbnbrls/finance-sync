"""Regression tests for connection sync database session ownership."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finance_sync.api.v1.sync import _run_connection_sync


@pytest.mark.asyncio
async def test_connection_sync_releases_request_session_before_provider_io() -> (
    None
):
    """Provider authentication must not hold the caller's DB connection."""
    db = MagicMock()
    db.close = AsyncMock()
    container = SimpleNamespace(
        settings=SimpleNamespace(),
        session_factory=MagicMock(),
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
            new=AsyncMock(return_value="run-1"),
        ),
        patch(
            "finance_sync.api.v1.sync._record_sync_audit",
            new=AsyncMock(),
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
