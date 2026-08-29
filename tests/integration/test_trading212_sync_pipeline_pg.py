"""End-to-end Trading212 sync tests with a mocked HTTP API and PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.trading212 import Trading212Connector
from finance_sync.db.uow import UnitOfWork
from finance_sync.models import (
    Account,
    Holding,
    Security,
    SyncRun,
    Tenant,
    Transaction,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator
from tests.connectors.fixtures.trading212_api_fixtures import (
    ACCOUNT_CASH_RESPONSE,
    ACCOUNT_INFO_RESPONSE,
    ORDER_HISTORY_RESPONSE,
    PORTFOLIO_RESPONSE,
    TRANSACTION_HISTORY_RESPONSE,
)

pytestmark = pytest.mark.integration

_NO_RECONCILIATION = SimpleNamespace(
    worker_job_reconciliation_after_sync_enabled=False
)


class Trading212PipelineTransport(httpx.MockTransport):
    """Mock the endpoints used by the full Trading212 sync pipeline."""

    def __init__(self, *, fail_transactions: bool = False) -> None:
        self.fail_transactions = fail_transactions
        self.requests: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        path = request.url.path
        if path == "/api/v0/equity/account/cash":
            return httpx.Response(200, json=ACCOUNT_CASH_RESPONSE)
        if path == "/api/v0/equity/account/info":
            return httpx.Response(200, json=ACCOUNT_INFO_RESPONSE)
        if path == "/api/v0/equity/portfolio":
            return httpx.Response(200, json=PORTFOLIO_RESPONSE)
        if path == "/api/v0/equity/history/orders":
            return httpx.Response(200, json=ORDER_HISTORY_RESPONSE)
        if path == "/api/v0/equity/history/transactions":
            if self.fail_transactions:
                return httpx.Response(
                    400, json={"error": "history unavailable"}
                )
            return httpx.Response(200, json=TRANSACTION_HISTORY_RESPONSE)
        return httpx.Response(404, json={"error": f"unexpected path: {path}"})


class PipelineTrading212Connector(Trading212Connector):
    """Trading212 connector wired to a per-test mock transport."""

    def __init__(
        self, config: ConnectorConfig, *, fail_transactions: bool = False
    ) -> None:
        self.transport = Trading212PipelineTransport(
            fail_transactions=fail_transactions
        )
        super().__init__(
            config,
            http_client=httpx.AsyncClient(
                base_url="https://live.trading212.com", transport=self.transport
            ),
        )


def _config() -> ConnectorConfig:
    return ConnectorConfig(
        provider_type="trading212",
        credentials={"api_key": "test-api-key"},
        options={"demo": False},
    )


def _orchestrator(
    session_factory: Any, tenant: Tenant, **kwargs: Any
) -> SyncOrchestrator:
    class ConnectorFactory(PipelineTrading212Connector):
        def __init__(self, config: ConnectorConfig) -> None:
            super().__init__(config, **kwargs)

    registry = ConnectorRegistry()
    registry.reload()
    registry.register_class("trading212", ConnectorFactory, replace=True)
    return SyncOrchestrator(
        session_factory=session_factory,
        registry=registry,
        tenant_id=str(tenant.id),
        settings=_NO_RECONCILIATION,
    )


@pytest.fixture
async def tenant(session_factory) -> Tenant:
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(
            Tenant(slug="trading212-pipeline", name="Trading212 pipeline")
        )


async def _counts(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        models = (Account, Security, Holding, Transaction, SyncRun)
        return {
            model.__name__: len(
                (await session.execute(select(model))).scalars().all()
            )
            for model in models
        }


class TestTrading212SyncPipeline:
    async def test_successful_sync_persists_accounts_holdings_and_transactions(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        assert result.holdings_synced == len(PORTFOLIO_RESPONSE)
        assert result.transactions_synced == (
            len(ORDER_HISTORY_RESPONSE["items"])
            + len(TRANSACTION_HISTORY_RESPONSE["items"])
        )
        counts = await _counts(session_factory)
        assert counts["Account"] == 1
        assert counts["Holding"] == len(PORTFOLIO_RESPONSE)
        assert counts["Transaction"] == result.transactions_synced

    async def test_authentication_failure_creates_failed_run_without_data(
        self, session_factory, tenant
    ) -> None:
        config = ConnectorConfig(provider_type="trading212", credentials={})
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212", config
        )

        assert result.status == SyncRunStatus.FAILED
        assert "api_key" in (result.error_message or "")
        counts = await _counts(session_factory)
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

    async def test_transaction_api_failure_rolls_back_resource_writes(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(
            session_factory, tenant, fail_transactions=True
        ).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.FAILED
        counts = await _counts(session_factory)
        # The account/resource unit of work is rolled back as one batch; the
        # failed resource unit of work must not leave partial account,
        # holdings, or transactions.
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

    async def test_repeating_identical_sync_is_idempotent(
        self, session_factory, tenant
    ) -> None:
        orchestrator = _orchestrator(session_factory, tenant)
        since = datetime(2024, 1, 1, tzinfo=UTC)
        first = await orchestrator.run_sync(
            "trading212", _config(), since=since
        )
        before = await _counts(session_factory)
        second = await orchestrator.run_sync(
            "trading212", _config(), since=since
        )
        after = await _counts(session_factory)

        assert first.status == SyncRunStatus.COMPLETED
        assert second.status == SyncRunStatus.COMPLETED
        assert after["Account"] == before["Account"]
        assert after["Security"] == before["Security"]
        # Holdings are time-versioned snapshots, so an identical later sync
        # legitimately creates one new snapshot per portfolio item.
        assert after["Holding"] == before["Holding"] + len(PORTFOLIO_RESPONSE)
        assert after["Transaction"] == before["Transaction"]
        assert after["SyncRun"] == before["SyncRun"] + 1
