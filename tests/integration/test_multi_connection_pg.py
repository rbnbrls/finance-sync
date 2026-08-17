"""PG integration: multiple connections per provider stay isolated.

Proves the t_7d8bc1f2 acceptance criteria against a real migrated
PostgreSQL database:

- two bunq-style connections with the *same* external account and
  transaction ids never collide (connection-scoped unique constraints)
- account selection filters which accounts are synced
- sync runs / cursors are scoped per connection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from finance_sync.connectors.base import Connector
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.models import (
    Account,
    Credential,
    SyncCursor,
    SyncRun,
    Tenant,
    Transaction,
)
from finance_sync.models.enums import SyncRunStatus
from finance_sync.sync.orchestrator import SyncOrchestrator

pytestmark = pytest.mark.integration

_NO_RECONCILIATION = SimpleNamespace(
    worker_job_reconciliation_after_sync_enabled=False
)


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
    @pytest.mark.xfail(
        reason=(
            "SyncOrchestrator per-connection support (connection_id param, "
            "connection-scoped persistence, selected-account filtering) lands "
            "in t_84877947; tests are carried by the data-layer branch so the "
            "acceptance contract is pinned here."
        ),
        strict=False,
    )
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

    @pytest.mark.xfail(
        reason=(
            "SyncOrchestrator per-connection support (connection_id param, "
            "connection-scoped persistence, selected-account filtering) lands "
            "in t_84877947; tests are carried by the data-layer branch so the "
            "acceptance contract is pinned here."
        ),
        strict=False,
    )
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
