"""Tests for the Actual Budget exporter service.

Uses mock AB client and mocks the exporter's DB-facing methods so
no real database is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
from finance_sync.exporter.actual_budget.exporter import (
    ActualBudgetExporter,
    ExportResult,
)

# ═══════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_mock_account(**kwargs) -> MagicMock:
    """Build a mock Account ORM instance."""
    acct = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "tenant_id": "tenant_001",
        "provider_key": "bunq",
        "external_account_id": "ext_acct_001",
        "name": "Checking Account",
        "account_type": "checking",
        "currency_code": "EUR",
        "is_active": True,
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(acct, k, v)
    return acct


def _make_mock_transaction(**kwargs) -> MagicMock:
    """Build a mock Transaction ORM instance."""
    txn = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "tenant_id": "tenant_001",
        "account_id": "acct_001",
        "provider_key": "bunq",
        "external_transaction_id": f"ext_txn_{uuid4().hex[:8]}",
        "amount": Decimal("-42.50"),
        "currency_code": "EUR",
        "occurred_at": datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
        "booked_at": datetime(2025, 6, 15, 14, 0, tzinfo=UTC),
        "transaction_type": "payment",
        "description": "Coffee Shop",
        "status": "booked",
        "revision": 1,
        "security_id": None,
        "provider_fingerprint": None,
        "amount_in_base": None,
        "base_currency_code": None,
        "fx_rate": None,
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(txn, k, v)
    return txn


class MockABClient:
    """Mock ActualBudgetClient that simulates a working AB connection."""

    def __init__(self, config: ActualBudgetConfig) -> None:
        self.config = config
        self._accounts: dict[str, dict] = {}
        self.created_transactions: list[dict] = []
        self.is_connected = True
        self.session = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_accounts(self) -> list[dict]:
        return list(self._accounts.values())

    async def get_account_by_name(self, name: str) -> dict | None:
        return self._accounts.get(name)

    async def create_account(
        self, name: str, *, off_budget=False, initial_balance=0.0
    ) -> dict:
        acct = {
            "id": str(uuid4()),
            "name": name,
            "offbudget": off_budget,
        }
        self._accounts[name] = acct
        return acct

    async def get_or_create_account(
        self, name: str, *, off_budget=False
    ) -> dict:
        existing = await self.get_account_by_name(name)
        if existing:
            return existing
        return await self.create_account(name, off_budget=off_budget)

    async def create_transaction(self, **kwargs) -> str | None:
        self.created_transactions.append(kwargs)
        return str(uuid4())

    async def import_transactions_batch(
        self, account: str, transactions: list[dict]
    ) -> int:
        self.created_transactions.extend(transactions)
        return len(transactions)

    async def commit(self) -> None:
        pass


@pytest.fixture
def ab_config() -> ActualBudgetConfig:
    return ActualBudgetConfig(
        server_url="http://localhost:5006",
        password="test-password",
        budget_name="Test Budget",
    )


@pytest.fixture
def exporter(ab_config) -> ActualBudgetExporter:
    """Exporter with a fully mocked session factory."""
    # Create a mock that behaves like async_sessionmaker
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.merge = AsyncMock()

    # session_factory is a callable returning the mock session
    session_factory = MagicMock(return_value=mock_session)

    return ActualBudgetExporter(
        session_factory=session_factory,
        ab_config=ab_config,
        tenant_id="tenant_001",
    )


# ═══════════════════════════════════════════════════════════════════════
# Tests for ExportResult
# ═══════════════════════════════════════════════════════════════════════


class TestExportResult:
    def test_construct_and_repr(self) -> None:
        r = ExportResult(
            status="completed",
            accounts_mapped=2,
            transactions_attempted=10,
            transactions_exported=9,
            transactions_failed=1,
            duration_s=3.14,
        )
        assert r.status == "completed"
        assert r.accounts_mapped == 2
        assert r.transactions_exported == 9
        assert r.transactions_failed == 1
        rep = repr(r)
        assert "completed" in rep
        assert "9/10" in rep

    def test_failed_result(self) -> None:
        r = ExportResult(
            status="failed",
            error_message="Connection refused",
        )
        assert r.status == "failed"
        assert r.error_message == "Connection refused"


# ═══════════════════════════════════════════════════════════════════════
# Tests for ActualBudgetExporter with mocked internals
# ═══════════════════════════════════════════════════════════════════════


class TestActualBudgetExporter:
    """Exporter tests with mocked DB and AB client."""

    @pytest.mark.asyncio
    async def test_run_export_no_transactions(self, exporter) -> None:
        """No transactions returns completed result with zero counts."""
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                exporter,
                "_last_export_time",
                return_value=datetime(2020, 1, 1, tzinfo=UTC),
            ),
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2020, 1, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0

    @pytest.mark.asyncio
    async def test_run_export_with_account_but_no_txns(self, exporter) -> None:
        """Account without recent transactions completes gracefully."""
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        mock_account = _make_mock_account()

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_account],
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 7, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0

    @pytest.mark.asyncio
    async def test_run_export_with_transactions(self, exporter) -> None:
        """Transactions should be mapped and imported into AB."""
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        mock_acct = _make_mock_account()
        mock_txns = [
            _make_mock_transaction(
                amount=Decimal("-50.00"),
                description="Test Payment",
            ),
            _make_mock_transaction(
                amount=Decimal("100.00"),
                description="Test Deposit",
                transaction_type="deposit",
            ),
        ]

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_update_export_delivery",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 2
        assert result.transactions_exported == 2
        assert result.accounts_mapped >= 1

    @pytest.mark.asyncio
    async def test_run_export_connection_failure(self, exporter) -> None:
        """Connection failure results in a failed export."""
        from finance_sync.exporter.actual_budget.client import (
            ActualBudgetConnectionError,
        )

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        class FailingMockClient(MockABClient):
            async def __aenter__(self):
                msg = "Connection refused"
                raise ActualBudgetConnectionError(msg)

        with (
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                FailingMockClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2020, 1, 1, tzinfo=UTC),
            )

        assert result.status == "failed"
        assert result.error_message is not None
        assert "Connection refused" in result.error_message

    @pytest.mark.asyncio
    async def test_run_export_filtered_account_ids(self, exporter) -> None:
        """Export respects account_ids filter."""
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        # Only return account A
        acct_a = _make_mock_account(name="Account A")
        mock_txn = _make_mock_transaction(account_id=acct_a.id)

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[acct_a],
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[mock_txn],
            ),
            patch.object(
                exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Account A"},
            ),
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_update_export_delivery",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
                account_ids=[acct_a.id],
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 1

    @pytest.mark.asyncio
    async def test_max_transactions_limit(self, exporter) -> None:
        """max_transactions should limit the export batch."""
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        mock_acct = _make_mock_account()
        mock_txns = [_make_mock_transaction() for _ in range(5)]

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(
                exporter,
                "_write_csv",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_update_export_delivery",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
                max_transactions=3,
            )

        assert result.status == "completed"
        # Should have only exported 3 out of 5
        assert result.transactions_attempted == 3


# ═══════════════════════════════════════════════════════════════════════
# Tests for ExportDelivery cursor
# ═══════════════════════════════════════════════════════════════════════


class TestExportDelivery:
    """Tests for the export delivery cursor (idempotency)."""

    @pytest.mark.asyncio
    async def test_update_delivery_creates_new(self, exporter) -> None:
        """_update_export_delivery creates a new ExportDelivery record."""
        account_id = str(uuid4())
        txn_ids = [str(uuid4()), str(uuid4())]

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        # execute returns a result mock where scalar_one_or_none() -> None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        await exporter._update_export_delivery(
            mock_session,
            account_id=account_id,
            transaction_ids=txn_ids,
        )

        # Should have added a new ExportDelivery
        mock_session.add.assert_called_once()
        delivery = mock_session.add.call_args[0][0]
        assert delivery.tenant_id == "tenant_001"
        assert delivery.account_id == account_id
        assert delivery.last_exported_transaction_id == txn_ids[-1]
        mock_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_delivery_updates_existing(self, exporter) -> None:
        """_update_export_delivery updates an existing ExportDelivery."""
        account_id = str(uuid4())
        txn_ids = [str(uuid4())]

        existing_delivery = MagicMock()
        existing_delivery.last_exported_transaction_id = "old_txn_id"
        existing_delivery.tenant_id = "tenant_001"
        existing_delivery.account_id = account_id

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_delivery
        mock_session.execute = AsyncMock(return_value=mock_result)

        await exporter._update_export_delivery(
            mock_session,
            account_id=account_id,
            transaction_ids=txn_ids,
        )

        # Should NOT have added a new record
        mock_session.add.assert_not_called()
        # Should have updated the existing record
        assert existing_delivery.last_exported_transaction_id == txn_ids[-1]

    @pytest.mark.asyncio
    async def test_update_delivery_empty_ids_is_noop(self, exporter) -> None:
        """Empty transaction_ids should be a no-op."""
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()

        await exporter._update_export_delivery(
            mock_session,
            account_id=str(uuid4()),
            transaction_ids=[],
        )

        mock_session.execute.assert_not_called()
        mock_session.add.assert_not_called()
        mock_session.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_delivery_returns_none_when_missing(
        self, exporter
    ) -> None:
        """_get_export_delivery returns None when no record exists."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await exporter._get_export_delivery(
            mock_session,
            account_id=str(uuid4()),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_delivery_returns_record(self, exporter) -> None:
        """_get_export_delivery returns the existing record."""
        account_id = str(uuid4())
        expected = MagicMock()
        expected.account_id = account_id
        expected.last_exported_transaction_id = "txn_123"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expected
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await exporter._get_export_delivery(
            mock_session,
            account_id=account_id,
        )

        assert result is not None
        assert result.account_id == account_id
        assert result.last_exported_transaction_id == "txn_123"
