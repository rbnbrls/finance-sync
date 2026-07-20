"""Contract test template for connector implementations.

Every connector **must** pass all tests in this file.  To use::

    import pytest
    from tests.connectors.contract_test_template import ConnectorContractTest

    class TestBunqConnector(ConnectorContractTest):
        @pytest.fixture
        def connector_config(self) -> ConnectorConfig:
            return ConnectorConfig(
                provider_type="bunq",
                credentials={"api_key": "test_key"},
                options={"sandbox": True},
            )

        @pytest.fixture
        def connector(self, connector_config: ConnectorConfig) -> Connector:
            from finance_sync.connectors.bunq import BunqConnector
            return BunqConnector(config=connector_config)

        @pytest.fixture
        def sample_raw_data(
            self,
        ) -> tuple[list[RawAccount], list[RawTransaction]]:
            # Return at least one account and transaction for transform tests
            ...
"""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.models import (
    RawAccount,
)

if TYPE_CHECKING:
    from finance_sync.connectors.base import Connector
    from finance_sync.connectors.models import (
        RawTransaction,
    )

pytestmark = pytest.mark.asyncio


class ConnectorContractTest:
    """Mixin that every connector implementation must pass.

    Subclasses **must** provide the following fixtures:

    * ``connector_config`` ->
      :class:`~finance_sync.connectors.models.ConnectorConfig`
    * ``connector`` -> :class:`~finance_sync.connectors.base.Connector`
    * ``sample_raw_data`` ->
      ``tuple[list[RawAccount], list[RawTransaction]]``
    """

    # ── Authentication ────────────────────────────────────────────────

    async def test_authenticate_success(self, connector: Connector) -> None:
        """Connector should authenticate without raising."""
        await connector.authenticate()

    async def test_authenticate_idempotent(self, connector: Connector) -> None:
        """Calling authenticate twice should be safe."""
        await connector.authenticate()
        await connector.authenticate()

    # ── Health ─────────────────────────────────────────────────────────

    async def test_health_returns_health(self, connector: Connector) -> None:
        """Health check should return a ConnectorHealth object."""
        health = await connector.health()
        assert health.provider_type == connector.name

    # ── Accounts ───────────────────────────────────────────────────────

    async def test_fetch_accounts_returns_list(
        self, connector: Connector
    ) -> None:
        """fetch_accounts should return a list of RawAccount."""
        await connector.authenticate()
        accounts = await connector.fetch_accounts()
        assert isinstance(accounts, list)
        if accounts:
            account = accounts[0]
            assert isinstance(account, RawAccount)  # type: ignore[unreachable]
            assert account.external_account_id
            assert account.name
            assert account.account_type

    async def test_fetch_accounts_idempotent(
        self, connector: Connector
    ) -> None:
        """Calling fetch_accounts twice should return consistent types."""
        await connector.authenticate()
        first = await connector.fetch_accounts()
        second = await connector.fetch_accounts()
        # Both should be lists of RawAccount (content may change)
        assert isinstance(first, list)
        assert isinstance(second, list)

    # ── Transactions ───────────────────────────────────────────────────

    async def test_fetch_transactions_returns_list(
        self, connector: Connector
    ) -> None:
        """fetch_transactions should return a list of RawTransaction."""
        await connector.authenticate()
        since = datetime.now(UTC) - timedelta(days=90)
        txns = await connector.fetch_transactions(since=since)
        assert isinstance(txns, list)
        if txns:
            txn: RawTransaction = txns[0]  # type: ignore[unreachable]
            assert txn.external_transaction_id
            assert txn.external_account_id

    async def test_fetch_transactions_with_account_filter(
        self, connector: Connector
    ) -> None:
        """fetch_transactions should accept an account_id filter."""
        await connector.authenticate()
        since = datetime.now(UTC) - timedelta(days=90)
        txns = await connector.fetch_transactions(
            since=since, account_id="test"
        )
        assert isinstance(txns, list)

    async def test_fetch_transactions_with_limit(
        self, connector: Connector
    ) -> None:
        """fetch_transactions should accept a limit parameter."""
        await connector.authenticate()
        since = datetime.now(UTC) - timedelta(days=90)
        txns = await connector.fetch_transactions(since=since, limit=10)
        assert isinstance(txns, list)
        if len(txns) > 0:
            assert len(txns) <= 10

    # ── Transform ──────────────────────────────────────────────────────

    async def test_transform_accounts_roundtrip(
        self,
        connector: Connector,
        sample_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawAccount to CanonicalAccountData."""
        raw_accounts, _ = sample_raw_data
        if not raw_accounts:
            pytest.skip("No sample raw accounts provided")

        canonical = connector.transform_accounts(raw_accounts)
        assert len(canonical) == len(raw_accounts)
        for ca in canonical:
            assert ca.provider_key == connector.name
            assert ca.external_account_id
            assert ca.account_type

    async def test_transform_transactions_roundtrip(
        self,
        connector: Connector,
        sample_raw_data: tuple[list[RawAccount], list[RawTransaction]],
    ) -> None:
        """Transform should map RawTransaction to CanonicalTransactionData."""
        _, raw_txns = sample_raw_data
        if not raw_txns:
            pytest.skip("No sample raw transactions provided")

        canonical = connector.transform_transactions(raw_txns)
        assert len(canonical) == len(raw_txns)
        for ct in canonical:
            assert ct.provider_key == connector.name
            assert ct.external_transaction_id
            assert ct.transaction_type
            assert ct.status

    # ── Name ───────────────────────────────────────────────────────────

    async def test_name_is_string(self, connector: Connector) -> None:
        """The name property should return a non-empty string."""
        assert isinstance(connector.name, str)
        assert len(connector.name) > 0
        assert connector.name == connector.config.provider_type
