"""PG integration: multiple connections per provider stay isolated.

Proves the t_7d8bc1f2 acceptance criteria against a real migrated
PostgreSQL database:

- two bunq-style connections with the *same* external account and
  transaction ids never collide — also when synced *simultaneously*
  (connection-scoped unique constraints)
- account selection filters which accounts are synced
- sync runs / cursors are scoped per connection
- the scheduler iterates connections independently: a failing
  connection never blocks its siblings and paused connections are
  skipped
- ``POST /api/v1/sync/connections/{connection_id}`` syncs exactly the
  specified connection (and 404s for unknown/foreign connections)
- Wealthfolio export omits deselected accounts
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance_sync.config.settings import Settings
from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.container import Container
from finance_sync.models import (
    Account,
    Credential,
    SyncCursor,
    SyncRun,
    Tenant,
    Transaction,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.services.auth import encrypt_credential
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.worker.jobs import sync_connector_job

pytestmark = pytest.mark.integration

_NO_RECONCILIATION = SimpleNamespace(
    worker_job_reconciliation_after_sync_enabled=False
)

_TEST_SECRET = "test-secret-key-at-least-16-chars"
_MASTER_KEY = "ab" * 32  # 64 hex chars → 32-byte AES-256 key


def _test_settings() -> Settings:
    """Real settings for integration runs: known encryption key, no
    auto-reconciliation, no retry delays."""
    return Settings(
        secret_key=_TEST_SECRET,
        master_encryption_key=_MASTER_KEY,
        database_url=None,
        redis_url=None,
        worker_retry_max_attempts=1,
        worker_retry_base_delay_s=0.1,
        worker_job_reconciliation_after_sync_enabled=False,
    )


def _encrypted_payload(
    settings: Settings, payload: dict[str, str] | None = None
) -> tuple[bytes, bytes]:
    """Envelope-encrypt a JSON credential payload with *settings*' key."""
    body = payload or {"api_key": "secret-value-9f8e7d6c"}
    return encrypt_credential(json.dumps(body), settings)


def _test_container(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> Container:
    """DI container wired to the integration session factory.

    Uses the session-scoped NullPool engine of the harness instead of
    ``Container.from_settings`` (which would spin up a second pooled
    engine, unsafe across pytest-asyncio event loops).
    """
    container = Container()
    container._settings = settings
    container._session_factory = session_factory
    return container


async def _seed_connection(
    session: AsyncSession,
    tenant: Tenant,
    settings: Settings,
    *,
    provider: str = "mock_multi",
    status: str = "active",
    selected_accounts: list[str] | None = None,
) -> Credential:
    """Create a credential row whose payload really decrypts with
    *settings* — required because the scheduler job and the sync API
    decrypt credentials through the app's AES-256-GCM envelope."""
    ciphertext, nonce = _encrypted_payload(settings)
    cred = Credential(
        tenant_id=tenant.id,
        provider_key=provider,
        encrypted_payload=ciphertext,
        nonce=nonce,
        status=status,
        selected_accounts=selected_accounts,
    )
    session.add(cred)
    await session.flush()
    await session.commit()
    return cred


class StaticConnector(Connector):
    """A Connector that returns fixed accounts/transactions.

    The registry instantiates connectors with ``cls(config=config)``
    only, so the fixture data lives at class level and is set per test
    before the connector is registered.
    """

    display_name = "Static (test)"

    #: Fixture data — assign per test before registering.
    accounts: list[RawAccount] = []
    transactions: list[RawTransaction] = []

    @property
    def name(self) -> str:
        """Provider key used for scoping (matches the registered name)."""
        return "mock_multi"

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self.supported_resources: frozenset[str] = frozenset()

    async def authenticate(self) -> None:
        return None

    async def health(self):
        from finance_sync.connectors.models import ConnectorHealth

        return ConnectorHealth(healthy=True, provider_type=self.name)

    async def fetch_accounts(self) -> list[RawAccount]:
        return list(self.accounts)

    async def fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        txns = self.transactions
        if account_id is not None:
            txns = [t for t in txns if t.external_account_id == account_id]
        return list(txns)

    async def _rate_limited_fetch_accounts(self) -> list[RawAccount]:
        return await self.fetch_accounts()

    async def _rate_limited_fetch_transactions(
        self,
        since: datetime,
        *,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[RawTransaction]:
        return await self.fetch_transactions(
            since, account_id=account_id, limit=limit
        )


def _raw_account(ext_id: str, name: str) -> RawAccount:
    return RawAccount(
        external_account_id=ext_id,
        name=name,
        account_type="checking",
        currency_code="EUR",
        current_balance=Decimal("100.00"),
    )


def _raw_txn(ext_id: str, account_id: str, amount: str) -> RawTransaction:
    return RawTransaction(
        external_transaction_id=ext_id,
        external_account_id=account_id,
        amount=Decimal(amount),
        currency_code="EUR",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        description=f"txn {ext_id}",
        transaction_type="payment",
        status="booked",
    )


class TestMultiConnectionIsolationPg:
    async def test_same_external_ids_do_not_collide(
        self,
        session_factory,
        session,
    ) -> None:
        """Two connections with identical external ids store two of each."""
        tenant = Tenant(name="multi-conn", slug="multi-conn")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        StaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        StaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        registry.register_class("mock_multi", StaticConnector, replace=True)

        def _make_config() -> ConnectorConfig:
            return ConnectorConfig(
                provider_type="mock_multi",
                credentials={"api_key": "x"},
                options={},
            )

        # ── First connection: syncs account ext_acc + txn ext_txn ─────
        conn_a = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        session.add(conn_a)
        await session.flush()
        await session.commit()

        orch_a = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=str(tenant.id),
            settings=_NO_RECONCILIATION,
        )
        result_a = await orch_a.run_sync(
            "mock_multi",
            _make_config(),
            connection_id=str(conn_a.id),
        )
        assert result_a.status == SyncRunStatus.COMPLETED

        # ── Second connection: SAME external ids ──────────────────────
        conn_b = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        session.add(conn_b)
        await session.flush()
        await session.commit()

        orch_b = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=str(tenant.id),
            settings=_NO_RECONCILIATION,
        )
        result_b = await orch_b.run_sync(
            "mock_multi",
            _make_config(),
            connection_id=str(conn_b.id),
        )
        assert result_b.status == SyncRunStatus.COMPLETED

        # ── Assertions ────────────────────────────────────────────────
        async with session_factory() as s:
            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert len(accounts) == 2, "one account per connection"
            assert {a.connection_id for a in accounts} == {
                str(conn_a.id),
                str(conn_b.id),
            }
            assert {a.external_account_id for a in accounts} == {"ext_acc"}

            txns = (
                await s.scalars(
                    select(Transaction).where(
                        Transaction.tenant_id == str(tenant.id),
                        Transaction.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert len(txns) == 2, "one transaction per connection"
            assert {t.connection_id for t in txns} == {
                str(conn_a.id),
                str(conn_b.id),
            }

            cursors = (
                await s.scalars(
                    select(SyncCursor).where(
                        SyncCursor.tenant_id == str(tenant.id),
                        SyncCursor.connector == "mock_multi",
                    )
                )
            ).all()
            assert len(cursors) == 2, "independent cursors per connection"
            assert {c.connection_id for c in cursors} == {
                str(conn_a.id),
                str(conn_b.id),
            }

            runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connector == "mock_multi",
                        SyncRun.connection_id.in_(
                            [str(conn_a.id), str(conn_b.id)]
                        ),
                    )
                )
            ).all()
            assert len(runs) == 2
            assert {r.connection_id for r in runs} == {
                str(conn_a.id),
                str(conn_b.id),
            }

    async def test_account_selection_filters_sync(
        self,
        session_factory,
        session,
    ) -> None:
        """Only selected accounts are synced for a connection."""
        tenant = Tenant(name="multi-conn-sel", slug="multi-conn-sel")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        registry.register_class("mock_multi", StaticConnector, replace=True)

        config = ConnectorConfig(
            provider_type="mock_multi",
            credentials={"api_key": "x"},
            options={},
        )

        conn = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
            selected_accounts=["acc_1"],
        )
        session.add(conn)
        await session.flush()
        await session.commit()

        # Register a connector that offers TWO accounts.
        class TwoAccountConnector(StaticConnector):
            accounts = [
                _raw_account("acc_1", "One"),
                _raw_account("acc_2", "Two"),
            ]
            transactions = [
                _raw_txn("txn_1", "acc_1", "-10.00"),
                _raw_txn("txn_2", "acc_2", "-20.00"),
            ]

        registry.register_class("mock_multi", TwoAccountConnector, replace=True)

        orch = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=str(tenant.id),
            settings=_NO_RECONCILIATION,
        )
        result = await orch.run_sync(
            "mock_multi",
            config,
            connection_id=str(conn.id),
            selected_accounts=["acc_1"],
        )
        assert result.status == SyncRunStatus.COMPLETED
        assert result.accounts_synced == 1
        assert result.transactions_synced == 1

        async with session_factory() as s:
            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.connection_id == str(conn.id),
                    )
                )
            ).all()
            assert [a.external_account_id for a in accounts] == ["acc_1"]


class SelectiveFailConnector(StaticConnector):
    """A StaticConnector that fails authenticate() for one connection."""

    #: connection_id that must fail authentication (set per test).
    fail_connection_id: str | None = None

    async def authenticate(self) -> None:
        if (
            self.fail_connection_id
            and self.config.connection_id == self.fail_connection_id
        ):
            msg = "Authentication failed for secret-value-9f8e7d6c"
            from finance_sync.connectors.exceptions import PermanentError

            raise PermanentError(msg)


class TestFailureIsolationPg:
    @pytest.mark.integration
    async def test_failing_connection_does_not_block_siblings(
        self,
        session_factory,
        session,
    ) -> None:
        """One failing connection never blocks the other; the failure is
        recorded (sanitised) on the connection row."""
        tenant = Tenant(name="multi-conn-fail", slug="multi-conn-fail")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        StaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        StaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        registry.register_class(
            "mock_multi", SelectiveFailConnector, replace=True
        )

        conn_a = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        conn_b = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        session.add_all([conn_a, conn_b])
        await session.flush()
        await session.commit()

        SelectiveFailConnector.fail_connection_id = str(conn_a.id)

        def _make_config(connection_id: str) -> ConnectorConfig:
            return ConnectorConfig(
                provider_type="mock_multi",
                credentials={"api_key": "secret-value-9f8e7d6c"},
                options={},
                connection_id=connection_id,
            )

        orch = SyncOrchestrator(
            session_factory=session_factory,
            registry=registry,
            tenant_id=str(tenant.id),
            settings=_NO_RECONCILIATION,
        )

        # The failing connection must not prevent the sibling from syncing.
        result_a = await orch.run_sync(
            "mock_multi",
            _make_config(str(conn_a.id)),
            connection_id=str(conn_a.id),
        )
        result_b = await orch.run_sync(
            "mock_multi",
            _make_config(str(conn_b.id)),
            connection_id=str(conn_b.id),
        )
        assert result_a.status == SyncRunStatus.FAILED
        assert result_b.status == SyncRunStatus.COMPLETED

        async with session_factory() as s:
            reloaded_a = await s.get(Credential, conn_a.id)
            reloaded_b = await s.get(Credential, conn_b.id)

            # Failure outcome recorded, secret scrubbed, error truncated.
            assert reloaded_a.last_attempt_at is not None
            assert reloaded_a.last_success_at is None
            assert reloaded_a.last_error is not None
            assert "secret-value-9f8e7d6c" not in reloaded_a.last_error
            assert "Authentication failed" in reloaded_a.last_error

            # Successful sibling recorded a success timestamp, no error.
            assert reloaded_b.last_attempt_at is not None
            assert reloaded_b.last_success_at is not None
            assert reloaded_b.last_error is None

            # Both runs exist; the failed one is observable and scoped.
            runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connector == "mock_multi",
                        SyncRun.connection_id.in_(
                            [str(conn_a.id), str(conn_b.id)]
                        ),
                    )
                )
            ).all()
            assert len(runs) == 2
            statuses = {r.connection_id: r.status for r in runs}
            assert statuses[str(conn_a.id)] == SyncRunStatus.FAILED
            assert statuses[str(conn_b.id)] == SyncRunStatus.COMPLETED

            # Only the successful connection stored data.
            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert [a.connection_id for a in accounts] == [str(conn_b.id)]


class TestExportSelectionPg:
    @pytest.mark.integration
    async def test_deselected_accounts_not_exported(
        self,
        session_factory,
        session,
    ) -> None:
        """Wealthfolio export only sees accounts present in the
        connection's selected_accounts."""
        from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
        from finance_sync.exporter.wealthfolio.exporter import (
            WealthfolioExporter,
        )
        from finance_sync.models.enums import AccountType

        tenant = Tenant(name="multi-conn-export", slug="multi-conn-export")
        session.add(tenant)
        await session.flush()

        # Connection with a pinned selection: only acc_1 is selected.
        conn_sel = Credential(
            tenant_id=tenant.id,
            provider_key="trading212",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
            selected_accounts=["acc_1"],
        )
        # Connection without a selection: everything is exported.
        conn_all = Credential(
            tenant_id=tenant.id,
            provider_key="trading212",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        session.add_all([conn_sel, conn_all])
        await session.flush()

        accounts = [
            Account(
                tenant_id=tenant.id,
                provider_key="trading212",
                connection_id=str(conn_sel.id),
                external_account_id="acc_1",
                name="Selected",
                account_type=AccountType.BROKERAGE,
                currency_code="EUR",
            ),
            Account(
                tenant_id=tenant.id,
                provider_key="trading212",
                connection_id=str(conn_sel.id),
                external_account_id="acc_2",
                name="Deselected",
                account_type=AccountType.BROKERAGE,
                currency_code="EUR",
            ),
            Account(
                tenant_id=tenant.id,
                provider_key="trading212",
                connection_id=str(conn_all.id),
                external_account_id="acc_3",
                name="Unrestricted",
                account_type=AccountType.BROKERAGE,
                currency_code="EUR",
            ),
            Account(
                tenant_id=tenant.id,
                provider_key="trading212",
                connection_id=None,
                external_account_id="acc_legacy",
                name="Legacy",
                account_type=AccountType.BROKERAGE,
                currency_code="EUR",
            ),
        ]
        session.add_all(accounts)
        await session.flush()
        await session.commit()

        exporter = WealthfolioExporter(
            session_factory=session_factory,
            wf_config=WealthfolioConfig(),
            tenant_id=str(tenant.id),
        )
        loaded = await exporter._load_accounts(None)

        exported = {account.external_account_id for account in loaded}
        # Deselected account is never exported; selected + unrestricted +
        # legacy rows are.
        assert "acc_2" not in exported
        assert {"acc_1", "acc_3", "acc_legacy"} <= exported


class ConcurrentStaticConnector(StaticConnector):
    """A StaticConnector that pauses in ``fetch_accounts`` so two
    concurrent syncs genuinely overlap on the event loop."""

    async def fetch_accounts(self) -> list[RawAccount]:
        await asyncio.sleep(0.05)
        return list(self.accounts)


class TestConcurrentSyncPg:
    @pytest.mark.integration
    async def test_concurrent_same_provider_syncs_no_data_loss_or_collision(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """Two same-provider connections synced *simultaneously* (asyncio
        gather, interleaved fetches, separate DB sessions) with identical
        external account/transaction ids: every row survives, scoped to
        its own connection — no data loss, no ID collisions."""
        tenant = Tenant(name="multi-conn-conc", slug="multi-conn-conc")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        ConcurrentStaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        ConcurrentStaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        registry.register_class(
            "mock_multi", ConcurrentStaticConnector, replace=True
        )

        def _config() -> ConnectorConfig:
            return ConnectorConfig(
                provider_type="mock_multi",
                credentials={"api_key": "x"},
                options={},
            )

        conn_a = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        conn_b = Credential(
            tenant_id=tenant.id,
            provider_key="mock_multi",
            encrypted_payload=b"\x00" * 16,
            nonce=b"\x00" * 12,
            status="active",
        )
        session.add_all([conn_a, conn_b])
        await session.flush()
        await session.commit()

        def _orch(connection_id: str) -> SyncOrchestrator:
            return SyncOrchestrator(
                session_factory=session_factory,
                registry=registry,
                tenant_id=str(tenant.id),
                settings=_NO_RECONCILIATION,
            )

        results = await asyncio.gather(
            _orch(str(conn_a.id)).run_sync(
                "mock_multi",
                _config(),
                connection_id=str(conn_a.id),
            ),
            _orch(str(conn_b.id)).run_sync(
                "mock_multi",
                _config(),
                connection_id=str(conn_b.id),
            ),
        )

        assert [r.status for r in results] == [
            SyncRunStatus.COMPLETED,
            SyncRunStatus.COMPLETED,
        ]
        # No data loss: both connections ingested their full payload.
        assert sum(r.accounts_synced for r in results) == 2
        assert sum(r.transactions_synced for r in results) == 2

        async with session_factory() as s:
            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert len(accounts) == 2, "one account per connection"
            assert {a.connection_id for a in accounts} == {
                str(conn_a.id),
                str(conn_b.id),
            }
            assert {a.external_account_id for a in accounts} == {"ext_acc"}

            txns = (
                await s.scalars(
                    select(Transaction).where(
                        Transaction.tenant_id == str(tenant.id),
                        Transaction.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert len(txns) == 2, "one transaction per connection"
            assert {t.connection_id for t in txns} == {
                str(conn_a.id),
                str(conn_b.id),
            }

            cursors = (
                await s.scalars(
                    select(SyncCursor).where(
                        SyncCursor.tenant_id == str(tenant.id),
                        SyncCursor.connector == "mock_multi",
                    )
                )
            ).all()
            assert len(cursors) == 2, "independent cursors per connection"
            assert {c.connection_id for c in cursors} == {
                str(conn_a.id),
                str(conn_b.id),
            }

            runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connector == "mock_multi",
                        SyncRun.connection_id.in_(
                            [str(conn_a.id), str(conn_b.id)]
                        ),
                    )
                )
            ).all()
            assert len(runs) == 2, "one run per connection"
            assert {r.connection_id for r in runs} == {
                str(conn_a.id),
                str(conn_b.id),
            }


class TestSchedulerJobPg:
    @pytest.mark.integration
    async def test_failing_connection_does_not_block_siblings(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The scheduler job (``sync_connector_job``) runs every active
        connection independently against real PostgreSQL: a connection
        that fails authentication never blocks its sibling, and the
        failure is recorded (sanitised) on the failing connection row
        while the sibling completes with ``last_success_at``."""
        settings = _test_settings()
        tenant = Tenant(name="multi-conn-job-fail", slug="multi-conn-job-fail")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        StaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        StaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        registry.register_class(
            "mock_multi", SelectiveFailConnector, replace=True
        )

        conn_a = await _seed_connection(session, tenant, settings)
        conn_b = await _seed_connection(session, tenant, settings)
        SelectiveFailConnector.fail_connection_id = str(conn_a.id)

        container = _test_container(session_factory, settings)
        with patch(
            "finance_sync.worker.jobs.ConnectorRegistry", return_value=registry
        ):
            summary = await sync_connector_job(container, "mock_multi")

        by_conn = {r["connection_id"]: r for r in summary["results"]}
        assert by_conn[str(conn_a.id)]["status"] == "failed"
        assert by_conn[str(conn_a.id)]["error"]  # surfaced, not swallowed
        assert by_conn[str(conn_b.id)]["status"] == "completed"
        assert summary["failed"] == 1
        assert summary["skipped"] == 0

        async with session_factory() as s:
            reloaded_a = await s.get(Credential, conn_a.id)
            reloaded_b = await s.get(Credential, conn_b.id)

            # Failure outcome recorded, secret scrubbed, error kept.
            assert reloaded_a.last_attempt_at is not None
            assert reloaded_a.last_success_at is None
            assert reloaded_a.last_error is not None
            assert "secret-value-9f8e7d6c" not in reloaded_a.last_error
            assert "Authentication failed" in reloaded_a.last_error

            # Successful sibling recorded a success timestamp, no error.
            assert reloaded_b.last_attempt_at is not None
            assert reloaded_b.last_success_at is not None
            assert reloaded_b.last_error is None

            runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connector == "mock_multi",
                        SyncRun.connection_id.in_(
                            [str(conn_a.id), str(conn_b.id)]
                        ),
                    )
                )
            ).all()
            assert len(runs) == 2
            statuses = {r.connection_id: r.status for r in runs}
            assert statuses[str(conn_a.id)] == SyncRunStatus.FAILED
            assert statuses[str(conn_b.id)] == SyncRunStatus.COMPLETED

            # Only the successful connection stored data.
            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.provider_key == "mock_multi",
                    )
                )
            ).all()
            assert [a.connection_id for a in accounts] == [str(conn_b.id)]

    @pytest.mark.integration
    async def test_paused_connection_skipped_by_scheduler(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """Paused connections are skipped by scheduler runs: no sync run,
        no data, no attempt timestamp — the summary still records the
        skip so operators can see it."""
        settings = _test_settings()
        tenant = Tenant(name="multi-conn-job-paused", slug="multi-conn-job-paused")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        StaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        StaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        registry.register_class("mock_multi", StaticConnector, replace=True)

        conn_active = await _seed_connection(session, tenant, settings)
        conn_paused = await _seed_connection(
            session, tenant, settings, status="paused"
        )

        container = _test_container(session_factory, settings)
        with patch(
            "finance_sync.worker.jobs.ConnectorRegistry", return_value=registry
        ):
            summary = await sync_connector_job(container, "mock_multi")

        by_conn = {r["connection_id"]: r for r in summary["results"]}
        assert by_conn[str(conn_paused.id)]["status"] == "skipped"
        assert by_conn[str(conn_paused.id)]["reason"] == "paused"
        assert by_conn[str(conn_active.id)]["status"] == "completed"
        assert summary["skipped"] == 1
        assert summary["failed"] == 0

        async with session_factory() as s:
            paused = await s.get(Credential, conn_paused.id)
            active = await s.get(Credential, conn_active.id)

            # The scheduler never touched the paused connection.
            assert paused.last_attempt_at is None
            assert paused.last_success_at is None
            assert paused.last_error is None
            assert active.last_success_at is not None

            paused_runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == str(conn_paused.id)
                    )
                )
            ).all()
            assert paused_runs == [], "paused connection must not run"

            active_runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == str(conn_active.id)
                    )
                )
            ).all()
            assert len(active_runs) == 1

            accounts = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id)
                    )
                )
            ).all()
            assert [a.connection_id for a in accounts] == [str(conn_active.id)]


class TestManualSyncEndpointPg:
    @pytest.mark.integration
    async def test_manual_sync_triggers_only_specified_connection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """``POST /api/v1/sync/connections/{connection_id}`` (real app,
        real PG, real credential decryption) syncs exactly that
        connection: the sibling connection is untouched."""
        import httpx

        from finance_sync.api.deps.auth import AuthContext, get_auth_context
        from finance_sync.app import create_app

        settings = _test_settings()
        tenant = Tenant(name="multi-conn-api", slug="multi-conn-api")
        session.add(tenant)
        await session.flush()

        registry = ConnectorRegistry()
        StaticConnector.accounts = [
            _raw_account("ext_acc", "Main Account"),
        ]
        StaticConnector.transactions = [
            _raw_txn("ext_txn", "ext_acc", "-10.00"),
        ]
        # The provider key must match StaticConnector.name — the base
        # transform stamps provider_key from ``self.name``.
        registry.register_class("mock_multi", StaticConnector, replace=True)

        conn_a = await _seed_connection(
            session, tenant, settings, provider="mock_multi"
        )
        conn_b = await _seed_connection(
            session, tenant, settings, provider="mock_multi"
        )

        app = create_app(settings=settings)
        app.state.container = _test_container(session_factory, settings)
        user = SimpleNamespace(
            id="user-1", tenant_id=str(tenant.id), role="admin"
        )
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            user=user
        )

        with patch(
            "finance_sync.api.v1.sync.ConnectorRegistry", return_value=registry
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    f"/api/v1/sync/connections/{conn_a.id}"
                )

        assert response.status_code == 202
        body = response.json()
        assert body["connection_id"] == str(conn_a.id)
        assert body["provider"] == "mock_multi"
        assert body["status"] == "completed"
        assert body["sync_run_id"] is not None
        assert body["accounts_synced"] == 1
        assert body["transactions_synced"] == 1

        async with session_factory() as s:
            runs_a = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == str(conn_a.id)
                    )
                )
            ).all()
            runs_b = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == str(conn_b.id)
                    )
                )
            ).all()
            assert len(runs_a) == 1
            assert runs_b == [], "the sibling connection must not be synced"

            accounts_a = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.connection_id == str(conn_a.id),
                    )
                )
            ).all()
            accounts_b = (
                await s.scalars(
                    select(Account).where(
                        Account.tenant_id == str(tenant.id),
                        Account.connection_id == str(conn_b.id),
                    )
                )
            ).all()
            assert len(accounts_a) == 1
            assert accounts_b == []

            cred_a = await s.get(Credential, conn_a.id)
            cred_b = await s.get(Credential, conn_b.id)
            assert cred_a.last_attempt_at is not None
            assert cred_a.last_success_at is not None
            assert cred_a.last_error is None
            # The sibling connection was never attempted.
            assert cred_b.last_attempt_at is None
            assert cred_b.last_success_at is None

    @pytest.mark.integration
    async def test_manual_sync_404_for_unknown_and_foreign_connection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """Unknown ids and connections owned by another tenant both
        return 404 and trigger nothing."""
        import httpx

        from finance_sync.api.deps.auth import AuthContext, get_auth_context
        from finance_sync.app import create_app

        settings = _test_settings()
        tenant = Tenant(name="multi-conn-api-404", slug="multi-conn-api-404")
        session.add(tenant)
        await session.flush()

        other_tenant = Tenant(name="other-tenant", slug="other-tenant")
        session.add(other_tenant)
        await session.flush()
        foreign_conn = await _seed_connection(
            session, other_tenant, settings, provider="mock_multi"
        )

        registry = ConnectorRegistry()
        registry.register_class("mock_multi", StaticConnector, replace=True)

        app = create_app(settings=settings)
        app.state.container = _test_container(session_factory, settings)
        user = SimpleNamespace(
            id="user-1", tenant_id=str(tenant.id), role="admin"
        )
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            user=user
        )

        with patch(
            "finance_sync.api.v1.sync.ConnectorRegistry", return_value=registry
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                unknown = await client.post(
                    f"/api/v1/sync/connections/{uuid4()}"
                )
                foreign = await client.post(
                    f"/api/v1/sync/connections/{foreign_conn.id}"
                )

        assert unknown.status_code == 404
        assert foreign.status_code == 404

        # Nothing was synced by either 404 attempt.
        async with session_factory() as s:
            runs = (
                await s.scalars(
                    select(SyncRun).where(
                        SyncRun.connection_id == str(foreign_conn.id)
                    )
                )
            ).all()
            assert runs == []
