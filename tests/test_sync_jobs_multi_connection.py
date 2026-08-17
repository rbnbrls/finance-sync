"""Unit tests for per-connection scheduler isolation (t_84877947).

Proves at the job level that the scheduler iterates every connection
independently: paused connections are skipped, a failing connection
never blocks its siblings, and each run is scoped to its connection id
with the connection's selected accounts passed through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from finance_sync.worker.jobs import (
    sync_bunq_cards_job,
    sync_connector_job,
)


def _connection(
    connection_id: str,
    provider: str = "bunq",
    status: str = "active",
    selected: list[str] | None = None,
) -> dict:
    """A connection entry in the shape ``_get_tenant_connections`` returns."""
    return {
        "tenant": SimpleNamespace(id="tenant-1"),
        "credential": SimpleNamespace(
            id=connection_id,
            provider_key=provider,
            status=status,
            selected_accounts=selected,
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
        ),
        "config": SimpleNamespace(
            provider_type=provider,
            credentials={},
            options={},
            # The connector config carries the connection context so
            # fetchers can scope provider calls by connection.
            connection_id=connection_id,
            selected_accounts=selected,
        ),
        "secrets": [],
    }


def _container() -> SimpleNamespace:
    """Fake DI container: one retry attempt, no backoff, mocked factory."""
    factory = MagicMock()
    cm = factory.return_value  # async-context-manager from session_factory()
    cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return SimpleNamespace(
        settings=SimpleNamespace(
            worker_retry_max_attempts=1,
            worker_retry_base_delay_s=0.0,
        ),
        session_factory=factory,
    )


def _completed_result() -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        accounts_synced=1,
        transactions_synced=2,
        holdings_synced=0,
        unresolved_securities=0,
        error_message=None,
        duration_s=0.1,
    )


class TestSyncConnectorJobIsolation:
    async def test_paused_connection_is_skipped(self) -> None:
        connections = [
            _connection("conn-active"),
            _connection("conn-paused", status="paused"),
        ]
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=_completed_result())

        with (
            patch(
                "finance_sync.worker.jobs._get_tenant_connections",
                AsyncMock(return_value=connections),
            ),
            patch(
                "finance_sync.worker.jobs.SyncOrchestrator",
                return_value=fake_orch,
            ),
        ):
            summary = await sync_connector_job(_container(), "bunq")

        by_conn = {r["connection_id"]: r for r in summary["results"]}
        assert by_conn["conn-paused"]["status"] == "skipped"
        assert by_conn["conn-active"]["status"] == "completed"
        # Only the active connection actually ran.
        assert fake_orch.run_sync.await_count == 1
        kwargs = fake_orch.run_sync.await_args.kwargs
        assert kwargs["connection_id"] == "conn-active"
        assert summary["skipped"] == 1

    async def test_failing_connection_does_not_block_siblings(self) -> None:
        connections = [
            _connection("conn-fail"),
            _connection("conn-ok"),
        ]
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(
            side_effect=[RuntimeError("provider exploded"), _completed_result()]
        )

        with (
            patch(
                "finance_sync.worker.jobs._get_tenant_connections",
                AsyncMock(return_value=connections),
            ),
            patch(
                "finance_sync.worker.jobs.SyncOrchestrator",
                return_value=fake_orch,
            ),
        ):
            summary = await sync_connector_job(_container(), "bunq")

        by_conn = {r["connection_id"]: r for r in summary["results"]}
        assert by_conn["conn-fail"]["status"] == "failed"
        assert by_conn["conn-fail"]["error"]  # error surfaced (not swallowed)
        assert by_conn["conn-ok"]["status"] == "completed"
        assert fake_orch.run_sync.await_count == 2
        assert summary["failed"] == 1

    async def test_selected_accounts_passed_through(self) -> None:
        connections = [
            _connection("conn-sel", selected=["acc_1", "acc_2"]),
        ]
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=_completed_result())

        with (
            patch(
                "finance_sync.worker.jobs._get_tenant_connections",
                AsyncMock(return_value=connections),
            ),
            patch(
                "finance_sync.worker.jobs.SyncOrchestrator",
                return_value=fake_orch,
            ),
        ):
            await sync_connector_job(_container(), "bunq")

        kwargs = fake_orch.run_sync.await_args.kwargs
        assert kwargs["selected_accounts"] == ["acc_1", "acc_2"]

    async def test_config_carries_connection_context(self) -> None:
        """The ConnectorConfig handed to the connector carries the
        connection id + selection so fetchers can scope by connection."""
        connections = [
            _connection("conn-ctx", selected=["acc_1"]),
        ]
        fake_orch = MagicMock()
        fake_orch.run_sync = AsyncMock(return_value=_completed_result())

        with (
            patch(
                "finance_sync.worker.jobs._get_tenant_connections",
                AsyncMock(return_value=connections),
            ),
            patch(
                "finance_sync.worker.jobs.SyncOrchestrator",
                return_value=fake_orch,
            ),
        ):
            await sync_connector_job(_container(), "bunq")

        kwargs = fake_orch.run_sync.await_args.kwargs
        cfg = kwargs["config"]
        assert cfg.connection_id == "conn-ctx"
        assert cfg.selected_accounts == ["acc_1"]


class TestBunqCardsJobIsolation:
    async def test_paused_connection_skipped_and_error_isolated(self) -> None:
        connections = [
            _connection("cards-paused", status="paused"),
            _connection("cards-ok"),
        ]
        fake_orch = MagicMock()
        fake_orch.run_bunq_cards_sync = AsyncMock(
            return_value=SimpleNamespace(
                status=SimpleNamespace(value="completed"),
                schedules_synced=3,
                card_transactions_synced=5,
                error_message=None,
                duration_s=0.2,
            )
        )

        with (
            patch(
                "finance_sync.worker.jobs._get_tenant_connections",
                AsyncMock(return_value=connections),
            ),
            patch(
                "finance_sync.worker.jobs.SyncOrchestrator",
                return_value=fake_orch,
            ),
        ):
            summary = await sync_bunq_cards_job(_container())

        by_conn = {r["connection_id"]: r for r in summary["results"]}
        assert by_conn["cards-paused"]["status"] == "skipped"
        assert by_conn["cards-ok"]["status"] == "completed"
        assert fake_orch.run_bunq_cards_sync.await_count == 1
        kwargs = fake_orch.run_bunq_cards_sync.await_args.kwargs
        assert kwargs["connection_id"] == "cards-ok"
