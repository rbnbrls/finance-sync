"""Tests for the Connector abstract base class."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_sync.connectors.exceptions import (
    PermanentError,
    TransientError,
)
from finance_sync.connectors.models import (
    ConnectorConfig,
    RawAccount,
    RawTransaction,
)
from finance_sync.connectors.rate_limiter import RateLimitPolicy

if TYPE_CHECKING:
    from finance_sync.connectors.base import Connector

pytestmark = pytest.mark.asyncio


class TestConnectorABC:
    """Tests for the abstract base — via MockConnector."""

    async def test_authenticate_success(
        self, mock_connector: Connector
    ) -> None:
        await mock_connector.authenticate()
        assert mock_connector.auth_calls == 1  # type: ignore[attr-defined]

    async def test_authenticate_permanent_failure(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        from tests.conftest import MockConnector

        conn = MockConnector(config=sample_connector_config, fail_auth=True)
        with pytest.raises(PermanentError, match="Mock auth failed"):
            await conn.authenticate()

    async def test_authenticate_transient_failure(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        from tests.conftest import MockConnector

        conn = MockConnector(
            config=sample_connector_config,
            fail_auth=True,
            transient_errors=True,
        )
        with pytest.raises(TransientError, match="Mock provider unavailable"):
            await conn.authenticate()

    async def test_fetch_accounts(self, mock_connector: Connector) -> None:
        accounts = await mock_connector.fetch_accounts()
        assert len(accounts) == 1
        assert accounts[0].name == "Main Checking"

    async def test_fetch_accounts_permanent_failure(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        from tests.conftest import MockConnector

        conn = MockConnector(config=sample_connector_config, fail_accounts=True)
        with pytest.raises(PermanentError, match="Mock accounts error"):
            await conn.fetch_accounts()

    async def test_fetch_transactions_since(
        self, mock_connector: Connector
    ) -> None:
        # All transactions are on 2025-06-01,
        # searching since 2025-05-01 should find them
        since = datetime(2025, 5, 1, tzinfo=UTC)
        txns = await mock_connector.fetch_transactions(since=since)
        assert len(txns) == 1

    async def test_fetch_transactions_since_no_results(
        self, mock_connector: Connector
    ) -> None:
        # Searching since 2025-07-01 should find nothing
        since = datetime(2025, 7, 1, tzinfo=UTC)
        txns = await mock_connector.fetch_transactions(since=since)
        assert len(txns) == 0

    async def test_fetch_transactions_filter_account(
        self, mock_connector: Connector
    ) -> None:
        since = datetime(2025, 1, 1, tzinfo=UTC)
        txns = await mock_connector.fetch_transactions(
            since=since, account_id="nonexistent"
        )
        assert len(txns) == 0

    async def test_fetch_transactions_permanent_failure(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        from tests.conftest import MockConnector

        conn = MockConnector(
            config=sample_connector_config, fail_transactions=True
        )
        since = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(PermanentError, match="Mock transactions error"):
            await conn.fetch_transactions(since=since)

    async def test_health_success(self, mock_connector: Connector) -> None:
        health = await mock_connector.health()
        assert health.healthy
        assert health.provider_type == "mock_provider"

    async def test_health_failure(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        from tests.conftest import MockConnector

        conn = MockConnector(config=sample_connector_config, fail_auth=True)
        health = await conn.health()
        assert not health.healthy

    async def test_transform_accounts(
        self, mock_connector: Connector, sample_raw_account: RawAccount
    ) -> None:
        canonical = mock_connector.transform_accounts([sample_raw_account])
        assert len(canonical) == 1
        ca = canonical[0]
        assert ca.provider_key == "mock_provider"
        assert ca.external_account_id == "acc_12345"
        assert ca.account_type == "checking"
        assert ca.current_balance == Decimal("1520.45")

    async def test_transform_transactions(
        self, mock_connector: Connector, sample_raw_transaction: RawTransaction
    ) -> None:
        canonical = mock_connector.transform_transactions(
            [sample_raw_transaction]
        )
        assert len(canonical) == 1
        ct = canonical[0]
        assert ct.provider_key == "mock_provider"
        assert ct.external_transaction_id == "txn_98765"
        assert ct.amount == Decimal("-42.50")
        # transaction_type defaults to "other" when raw has None
        assert ct.transaction_type == "purchase"
        assert ct.status == "booked"

    async def test_transform_transactions_default_type(
        self, mock_connector: Connector
    ) -> None:
        """Raw transactions with no type get 'other' as canonical type."""
        raw = RawTransaction(
            external_transaction_id="tx_1",
            external_account_id="acc_1",
            amount=Decimal(100),
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        canonical = mock_connector.transform_transactions([raw])
        assert canonical[0].transaction_type == "other"
        assert canonical[0].status == "pending"

    async def test_rate_limited_fetch_accounts_with_retry_on_transient(
        self, sample_connector_config: ConnectorConfig
    ) -> None:
        """Rate-limited fetch should retry on transient errors."""
        from tests.conftest import MockConnector

        policy = RateLimitPolicy(
            max_requests=100, max_retries=3, backoff_base=0.01
        )
        conn = MockConnector(
            config=sample_connector_config,
            fail_accounts=True,
            transient_errors=True,
            rate_limit_policy=policy,
        )
        with pytest.raises(TransientError, match="retries exhausted"):
            await conn._rate_limited_fetch_accounts()
        # Should have made retries + 1 attempts
        assert conn.fetch_accounts_calls >= policy.max_retries + 1
