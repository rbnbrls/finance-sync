"""SyncOrchestrator integration tests against real PostgreSQL.

Port of the pipeline tests from ``tests/test_sync_orchestrator.py``
(which use a mocked UoW / SQLite test models) to a real, migrated
PostgreSQL database: a full sync run persists SyncRun / Account /
Transaction rows and emits transactional outbox messages, all inside a
single committed UoW transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.db.uow import UnitOfWork
from finance_sync.models import (
    Account,
    OutboxMessage,
    SyncRun,
    Tenant,
    Transaction,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator

pytestmark = pytest.mark.integration

# Disable the auto-reconciliation step so the sync pipeline tests stay
# focused on the ingestion path (reconciliation has its own suite).
_NO_RECONCILIATION = SimpleNamespace(
    worker_job_reconciliation_after_sync_enabled=False
)


class StaticMockConnector(Connector):
    """A Connector-subclass mock that returns fixed accounts/transactions."""

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        accounts: list[RawAccount] | None = None,
        transactions: list[RawTransaction] | None = None,
        fail_auth: bool = False,
        fail_accounts: bool = False,
    ) -> None:
        super().__init__(config)
        self._accounts = accounts or []
        self._transactions = transactions or []
        self._fail_auth = fail_auth
        self._fail_accounts = fail_accounts

    @property
    def name(self) -> str:
        return self.config.provider_type

    async def authenticate(self) -> None:
        if self._fail_auth:
            msg = "Mock auth failed"
            raise PermanentError(msg)

    async def fetch_accounts(self) -> list[RawAccount]:
        if self._fail_accounts:
            msg = "Mock accounts unavailable"
            raise PermanentError(msg)
        return self._accounts

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        filtered = self._transactions
        if account_id:
            filtered = [
                t for t in filtered if t.external_account_id == account_id
            ]
        if limit and limit < len(filtered):
            filtered = filtered[:limit]
        return [t for t in filtered if t.occurred_at >= since]


@pytest.fixture
async def tenant(session_factory) -> Tenant:
    """A real Tenant row the sync pipeline's FKs point at."""
    async with session_factory() as session, UnitOfWork(session) as uow:
        return await uow.tenants.add(Tenant(slug="it-tenant", name="IT Tenant"))


@pytest.fixture
def sample_raw_account() -> RawAccount:
    return RawAccount(
        external_account_id="acc_12345",
        name="Main Checking",
        account_type="checking",
        account_subtype=None,
        currency_code="EUR",
        current_balance=Decimal("1520.45"),
        available_balance=Decimal("1480.00"),
        iso_currency_code="EUR",
        provider_metadata={"iban": "NL00BANK0123456789"},
    )


@pytest.fixture
def sample_raw_transactions() -> list[RawTransaction]:
    # Within the orchestrator's default 90-day `since` window (now = 2026-08)
    return [
        RawTransaction(
            external_transaction_id=f"txn_{i}",
            external_account_id="acc_12345",
            amount=Decimal("-42.50"),
            currency_code="EUR",
            occurred_at=datetime(2026, 6, i, 12, 30, tzinfo=UTC),
            booked_at=datetime(2026, 6, i, 14, 0, tzinfo=UTC),
            description=f"Coffee {i}",
            transaction_type="purchase",
            status="booked",
            provider_fingerprint=f"hash_{i}",
        )
        for i in range(1, 4)
    ]


@pytest.fixture
def mock_connector_cls_factory(sample_raw_account, sample_raw_transactions):
    """Build a connector class pre-loaded with fixture data."""

    def _build(**kwargs) -> type[StaticMockConnector]:
        defaults = {
            "accounts": [sample_raw_account],
            "transactions": sample_raw_transactions,
        }
        defaults.update(kwargs)

        class _Mock(StaticMockConnector):
            def __init__(self, config: ConnectorConfig) -> None:
                super().__init__(config, **defaults)

        return _Mock

    return _build


def _make_orchestrator(
    session_factory,
    tenant: Tenant,
    connector_cls: type[Connector],
) -> SyncOrchestrator:
    registry = ConnectorRegistry()
    registry.register_class("mock_provider", connector_cls)
    return SyncOrchestrator(
        session_factory=session_factory,
        registry=registry,
        tenant_id=str(tenant.id),
        settings=_NO_RECONCILIATION,
    )


def _config() -> ConnectorConfig:
    return ConnectorConfig(
        provider_type="mock_provider",
        credentials={"api_key": "test_key"},
        options={"sandbox": True},
    )


class TestSyncPipelinePg:
    async def test_full_pipeline_persists_everything(
        self,
        session_factory,
        tenant,
        mock_connector_cls_factory,
    ) -> None:
        orchestrator = _make_orchestrator(
            session_factory, tenant, mock_connector_cls_factory()
        )
        result = await orchestrator.run_sync("mock_provider", _config())

        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        assert result.transactions_synced == 3

        # SyncRun persisted
        async with session_factory() as session:
            runs = (await session.execute(select(SyncRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == SyncRunStatus.COMPLETED
            assert runs[0].connector == "mock_provider"
            assert runs[0].items_processed == 4

            accounts = (await session.execute(select(Account))).scalars().all()
            assert len(accounts) == 1
            assert accounts[0].external_account_id == "acc_12345"
            assert accounts[0].tenant_id == tenant.id

            transactions = (
                (await session.execute(select(Transaction))).scalars().all()
            )
            assert len(transactions) == 3

            # Outbox messages emitted for account + transactions
            outbox = (
                (await session.execute(select(OutboxMessage))).scalars().all()
            )
            event_types = {m.event_type for m in outbox}
            assert "account.created" in event_types
            assert "transaction.created" in event_types
            assert len(outbox) == 4

    async def test_rerun_is_idempotent(
        self,
        session_factory,
        tenant,
        mock_connector_cls_factory,
    ) -> None:
        """A second sync with identical provider data upserts, not duplicates."""
        orchestrator = _make_orchestrator(
            session_factory, tenant, mock_connector_cls_factory()
        )
        first = await orchestrator.run_sync("mock_provider", _config())
        second = await orchestrator.run_sync("mock_provider", _config())

        assert first.status == SyncRunStatus.COMPLETED
        assert second.status == SyncRunStatus.COMPLETED

        async with session_factory() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            assert len(accounts) == 1  # upserted, not duplicated

            transactions = (
                (await session.execute(select(Transaction))).scalars().all()
            )
            assert len(transactions) == 3  # upserted, not duplicated

            runs = (await session.execute(select(SyncRun))).scalars().all()
            assert len(runs) == 2  # two distinct sync runs

    async def test_permanent_error_marks_run_failed_and_rolls_back(
        self, session_factory, tenant, mock_connector_cls_factory
    ) -> None:
        orchestrator = _make_orchestrator(
            session_factory,
            tenant,
            mock_connector_cls_factory(fail_auth=True),
        )
        result = await orchestrator.run_sync("mock_provider", _config())

        assert result.status == SyncRunStatus.FAILED
        assert "Mock auth failed" in (result.error_message or "")

        async with session_factory() as session:
            runs = (await session.execute(select(SyncRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == SyncRunStatus.FAILED

            # No accounts / transactions were written (UoW rolled back)
            accounts = (await session.execute(select(Account))).scalars().all()
            transactions = (
                (await session.execute(select(Transaction))).scalars().all()
            )
            assert accounts == []
            assert transactions == []

    async def test_account_failure_rolls_back_sync_run(
        self, session_factory, tenant, mock_connector_cls_factory
    ) -> None:
        """A provider error mid-pipeline rolls back the whole UoW."""
        orchestrator = _make_orchestrator(
            session_factory,
            tenant,
            mock_connector_cls_factory(fail_accounts=True),
        )
        result = await orchestrator.run_sync("mock_provider", _config())

        assert result.status == SyncRunStatus.FAILED

        async with session_factory() as session:
            runs = (await session.execute(select(SyncRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == SyncRunStatus.FAILED

            accounts = (await session.execute(select(Account))).scalars().all()
            transactions = (
                (await session.execute(select(Transaction))).scalars().all()
            )
            outbox = (
                (await session.execute(select(OutboxMessage))).scalars().all()
            )
            assert accounts == []
            assert transactions == []
            assert outbox == []
