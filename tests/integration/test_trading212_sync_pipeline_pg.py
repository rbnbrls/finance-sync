"""End-to-end Trading212 sync tests with a mocked HTTP API and PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.trading212 import Trading212Connector
from finance_sync.db.uow import UnitOfWork
from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter
from finance_sync.models import (
    Account,
    Holding,
    Security,
    SyncRun,
    Tenant,
    Transaction,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.services.account_selection import filter_accounts
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.persistence import SyncPersistence
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

    last_instance: Trading212PipelineTransport

    def __init__(
        self,
        *,
        fail_transactions: bool = False,
        fail_mapping: bool = False,
        retry_transactions_once: bool = False,
        empty_transactions: bool = False,
        malformed_portfolio: bool = False,
    ) -> None:
        self.fail_transactions = fail_transactions
        self.fail_mapping = fail_mapping
        self.retry_transactions_once = retry_transactions_once
        self.empty_transactions = empty_transactions
        self.malformed_portfolio = malformed_portfolio
        self.requests: list[str] = []
        self.transaction_attempts = 0
        type(self).last_instance = self
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        path = request.url.path
        if path == "/api/v0/equity/account/cash":
            return httpx.Response(200, json=ACCOUNT_CASH_RESPONSE)
        if path == "/api/v0/equity/account/info":
            return httpx.Response(200, json=ACCOUNT_INFO_RESPONSE)
        if path == "/api/v0/equity/portfolio":
            if self.malformed_portfolio:
                return httpx.Response(200, json={"unexpected": "shape"})
            return httpx.Response(200, json=PORTFOLIO_RESPONSE)
        if path == "/api/v0/equity/history/orders":
            return httpx.Response(200, json=ORDER_HISTORY_RESPONSE)
        if path == "/api/v0/equity/history/transactions":
            self.transaction_attempts += 1
            if self.retry_transactions_once and self.transaction_attempts == 1:
                return httpx.Response(
                    503, json={"error": "temporarily unavailable"}
                )
            if self.fail_transactions:
                return httpx.Response(
                    400, json={"error": "history unavailable"}
                )
            if self.empty_transactions:
                return httpx.Response(
                    200, json={"items": [], "nextPagePath": None}
                )
            return httpx.Response(200, json=TRANSACTION_HISTORY_RESPONSE)
        return httpx.Response(404, json={"error": f"unexpected path: {path}"})


class PipelineTrading212Connector(Trading212Connector):
    """Trading212 connector wired to a per-test mock transport."""

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        fail_transactions: bool = False,
        fail_mapping: bool = False,
        retry_transactions_once: bool = False,
        empty_transactions: bool = False,
        malformed_portfolio: bool = False,
    ) -> None:
        self.transport = Trading212PipelineTransport(
            fail_transactions=fail_transactions,
            fail_mapping=fail_mapping,
            retry_transactions_once=retry_transactions_once,
            empty_transactions=empty_transactions,
            malformed_portfolio=malformed_portfolio,
        )
        self.fail_mapping = fail_mapping
        super().__init__(
            config,
            http_client=httpx.AsyncClient(
                base_url="https://live.trading212.com", transport=self.transport
            ),
        )

    def transform_transactions(self, raw):
        if self.fail_mapping:
            message = (
                "Trading212 transaction mapping failed: unsupported payload"
            )
            raise PermanentError(message)
        return super().transform_transactions(raw)


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
    async def test_selected_account_missing_from_provider_fails_without_writes(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212",
            _config(),
            since=datetime(2024, 1, 1, tzinfo=UTC),
            selected_accounts=["changed-provider-account-id"],
        )

        assert result.status == SyncRunStatus.FAILED
        assert result.error_category == "validation"
        assert result.accounts_synced == 0
        counts = await _counts(session_factory)
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

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
        assert counts["Security"] == len(PORTFOLIO_RESPONSE)
        assert counts["Holding"] == len(PORTFOLIO_RESPONSE)
        assert counts["Transaction"] == result.transactions_synced
        assert counts["SyncRun"] == 1

        async with session_factory() as session:
            account = (await session.scalars(select(Account))).one()
            assert account.external_account_id == "12345678"
            assert account.name == "Trading212"
            assert account.account_type == "brokerage"
            assert account.currency_code == "EUR"
            assert account.current_balance == Decimal("10000.50")
            assert account.net_asset_value == Decimal("19626.00")

            holdings = (
                await session.scalars(
                    select(Holding).where(Holding.account_id == account.id)
                )
            ).all()
            assert {h.quantity for h in holdings} == {
                Decimal("10.0"),
                Decimal("5.0"),
                Decimal("50.0"),
            }
            assert {h.currency_code for h in holdings} == {"EUR"}
            assert {h.source for h in holdings} == {"provider_sync"}

            transactions = (
                await session.scalars(
                    select(Transaction).where(
                        Transaction.account_id == account.id
                    )
                )
            ).all()
            assert {t.external_transaction_id for t in transactions} == {
                "order_10000001",
                "order_10000002",
                "order_10000003",
                "order_10000004",
                "txn_20000001",
                "txn_20000002",
                "txn_20000003",
                "txn_20000004",
                "txn_20000005",
                "txn_20000006",
            }
            assert {t.currency_code for t in transactions} == {"EUR"}
            assert {t.status for t in transactions} == {"booked", "pending"}

    async def test_selected_account_filters_trading212_resources(
        self, session_factory, tenant
    ) -> None:
        """A selection excluding provider accounts fails without writes."""
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212",
            _config(),
            since=datetime(2024, 1, 1, tzinfo=UTC),
            selected_accounts=["different-account"],
        )

        assert result.status == SyncRunStatus.FAILED
        assert result.error_category == "validation"
        assert result.accounts_synced == 0
        assert result.holdings_synced == 0
        assert result.transactions_synced == 0
        counts = await _counts(session_factory)
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

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
        assert "HTTP 400" in (result.error_message or "")
        assert result.error_category == "validation"
        counts = await _counts(session_factory)
        # The account/resource unit of work is rolled back as one batch; the
        # failed resource unit of work must not leave partial account,
        # holdings, or transactions.
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

    async def test_mapping_failure_is_failed_with_actionable_error(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(
            session_factory, tenant, fail_mapping=True
        ).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.FAILED
        assert result.error_category == "data_mapping"
        assert "mapping" in (result.error_message or "").lower()
        counts = await _counts(session_factory)
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

    async def test_database_failure_is_failed_with_actionable_error(
        self, session_factory, tenant, monkeypatch
    ) -> None:
        from finance_sync.sync import orchestrator as orchestrator_module

        class FailingPersistence(orchestrator_module.SyncPersistence):
            async def persist_account(self, *args, **kwargs):
                statement = "insert account"
                raise IntegrityError(
                    statement,
                    {},
                    ValueError("duplicate Trading212 account"),
                )

        monkeypatch.setattr(
            orchestrator_module, "SyncPersistence", FailingPersistence
        )
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.FAILED
        assert result.error_category == "database"
        assert result.error_message == "Database error while syncing"
        counts = await _counts(session_factory)
        assert counts["Account"] == 0
        assert counts["SyncRun"] == 1

    async def test_selected_account_is_persisted_and_export_filter_can_find_it(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212",
            _config(),
            since=datetime(2024, 1, 1, tzinfo=UTC),
            selected_accounts=["12345678"],
        )

        assert result.status == SyncRunStatus.COMPLETED
        async with session_factory() as session:
            accounts = list((await session.execute(select(Account))).scalars())
            assert len(accounts) == 1
            assert accounts[0].external_account_id == "12345678"
            visible = await filter_accounts(session, str(tenant.id), accounts)
            assert visible == accounts
        exporter = WealthfolioExporter(
            session_factory=session_factory,
            wf_config=WealthfolioConfig(),
            tenant_id=str(tenant.id),
        )
        visible = await exporter._load_accounts(None)
        assert [account.external_account_id for account in visible] == [
            "12345678"
        ]
        async with session_factory() as session:
            account = visible[0]
            holdings = (
                (
                    await session.execute(
                        select(Holding).where(Holding.account_id == account.id)
                    )
                )
                .scalars()
                .all()
            )
            transactions = (
                (
                    await session.execute(
                        select(Transaction).where(
                            Transaction.account_id == account.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(holdings) == len(PORTFOLIO_RESPONSE)
            assert len(transactions) == (
                len(ORDER_HISTORY_RESPONSE["items"])
                + len(TRANSACTION_HISTORY_RESPONSE["items"])
            )

    async def test_transient_transaction_failure_is_retried_and_completes(
        self, session_factory, tenant, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "finance_sync.connectors.rate_limiter.RateLimiter.backoff_delay",
            lambda self, attempt: 0.0,
        )
        result = await _orchestrator(
            session_factory, tenant, retry_transactions_once=True
        ).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.transactions_synced > 0
        transport = Trading212PipelineTransport.last_instance
        assert transport.transaction_attempts == 2

    async def test_empty_transaction_response_still_completes_with_account_and_holdings(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(
            session_factory, tenant, empty_transactions=True
        ).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        # Orders are normalized as transactions; the transaction-history
        # endpoint being empty does not remove those order transactions.
        assert result.transactions_synced == len(
            ORDER_HISTORY_RESPONSE["items"]
        )
        counts = await _counts(session_factory)
        assert counts["Account"] == 1
        assert counts["Holding"] == len(PORTFOLIO_RESPONSE)

    async def test_malformed_portfolio_response_is_failed_without_partial_data(
        self, session_factory, tenant
    ) -> None:
        result = await _orchestrator(
            session_factory, tenant, malformed_portfolio=True
        ).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.FAILED
        assert result.error_message
        counts = await _counts(session_factory)
        assert counts["Account"] == 0
        assert counts["Holding"] == 0
        assert counts["Transaction"] == 0
        assert counts["SyncRun"] == 1

    async def test_persistence_failure_is_failed_without_partial_data(
        self, session_factory, tenant, monkeypatch
    ) -> None:
        async def fail_persist(*args, **kwargs):
            msg = "database write failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(SyncPersistence, "persist_account", fail_persist)
        result = await _orchestrator(session_factory, tenant).run_sync(
            "trading212", _config(), since=datetime(2024, 1, 1, tzinfo=UTC)
        )

        assert result.status == SyncRunStatus.FAILED
        # Internal errors are redacted in the public result; the original
        # exception is retained by the GlitchTip event.
        assert result.error_message == "Sync failed due to an internal error"
        counts = await _counts(session_factory)
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
