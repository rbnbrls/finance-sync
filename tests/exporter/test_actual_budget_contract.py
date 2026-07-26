"""Contract tests for the Actual Budget exporter.

Validates the exporter against the contract suite defined in
:mod:`tests.exporter.contract_test_template` using consumer-side
fixtures (finance-sync accounts and transactions) and provider-side
expectations (Actual Budget API behaviour).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
from finance_sync.exporter.actual_budget.exporter import (
    ActualBudgetExporter,
    ExportResult,
)
from finance_sync.exporter.actual_budget.transaction_mapper import (
    map_transaction,
)
from tests.exporter.contract_test_template import (
    ExporterConfigContractTest,
    ExportLifecycleContractTest,
    ExportResultContractTest,
    TransactionMappingContractTest,
)
from tests.exporter.fixtures.ab_fixtures import (
    AB_ACCOUNT_CHECKING,
    AB_ACCOUNT_SAVINGS,
    AB_TRANSACTION_DEPOSIT,
    AB_TRANSACTION_FEE,
    AB_TRANSACTION_FX,
    AB_TRANSACTION_INTEREST,
    AB_TRANSACTION_PAYMENT,
    AB_TRANSACTION_PENDING,
    AB_TRANSACTION_WITHDRAWAL,
    TRANSACTION_MAP_TEST_CASES,
)

# ═══════════════════════════════════════════════════════════════════════
# Config contract
# ═══════════════════════════════════════════════════════════════════════


class TestActualBudgetConfig(ExporterConfigContractTest):
    """Config construction and defaults."""

    @pytest.fixture
    def exporter_config(self) -> ActualBudgetConfig:
        return ActualBudgetConfig(
            server_url="http://localhost:5006",
            password="test-password",
            budget_name="Test Budget",
        )


# ═══════════════════════════════════════════════════════════════════════
# Export result contract
# ═══════════════════════════════════════════════════════════════════════


class TestActualBudgetResult(ExportResultContractTest):
    """ExportResult construction and semantics."""

    @pytest.fixture
    def completed_result(self) -> ExportResult:
        return ExportResult(
            status="completed",
            accounts_mapped=2,
            transactions_attempted=10,
            transactions_exported=9,
            transactions_failed=1,
            duration_s=3.14,
        )

    @pytest.fixture
    def failed_result(self) -> ExportResult:
        return ExportResult(
            status="failed",
            error_message="Connection refused",
        )


# ═══════════════════════════════════════════════════════════════════════
# Transaction mapping contract
# ═══════════════════════════════════════════════════════════════════════


class TestABTransactionMapping(TransactionMappingContractTest):
    """Canonical → Actual Budget transaction mapping."""

    @pytest.fixture
    def map_function(self):
        return lambda txn: map_transaction(txn, ab_account_name="AB Checking")

    @pytest.fixture
    def map_test_cases(self) -> list[dict]:
        return TRANSACTION_MAP_TEST_CASES

    # ── AB-specific mapping tests ───────────────────────────────────

    def test_map_payment_to_cents(self) -> None:
        """Payment amount should convert to integer cents."""
        result = map_transaction(
            AB_TRANSACTION_PAYMENT, ab_account_name="AB Checking"
        )
        assert result["amount"] == -4250  # -42.50 EUR

    def test_map_deposit_to_cents(self) -> None:
        """Deposit amount should convert to positive integer cents."""
        result = map_transaction(
            AB_TRANSACTION_DEPOSIT, ab_account_name="AB Checking"
        )
        assert result["amount"] == 150000  # 1500.00 EUR

    def test_map_includes_imported_id(self) -> None:
        """Mapped transaction should include an imported_id for dedup."""
        result = map_transaction(
            AB_TRANSACTION_PAYMENT, ab_account_name="AB Checking"
        )
        assert result["imported_id"].startswith("fs_")
        assert (
            AB_TRANSACTION_PAYMENT.external_transaction_id
            in result["imported_id"]
        )

    def test_map_fx_in_notes(self) -> None:
        """Multi-currency transactions should include FX info in notes."""
        result = map_transaction(
            AB_TRANSACTION_FX, ab_account_name="AB Investment"
        )
        notes = result.get("notes", "")
        assert "FX" in notes
        assert "USD" in notes
        assert "EUR" in notes

    def test_map_pending_not_cleared(self) -> None:
        """Pending status maps to cleared=False."""
        result = map_transaction(
            AB_TRANSACTION_PENDING, ab_account_name="AB Checking"
        )
        assert result["cleared"] is False

    def test_map_booked_is_cleared(self) -> None:
        """Booked status maps to cleared=True."""
        result = map_transaction(
            AB_TRANSACTION_PAYMENT, ab_account_name="AB Checking"
        )
        assert result["cleared"] is True

    def test_map_fee_notes(self) -> None:
        """Fee transactions should have 'Type: fee' in notes."""
        result = map_transaction(
            AB_TRANSACTION_FEE, ab_account_name="AB Checking"
        )
        notes = result.get("notes", "")
        assert "Monthly account fee" in notes or not notes

    def test_map_interest_notes(self) -> None:
        """Interest transactions should include description in notes."""
        result = map_transaction(
            AB_TRANSACTION_INTEREST, ab_account_name="AB Savings"
        )
        assert result["notes"] is None or "Interest" in result["notes"]

    def test_map_withdrawal_amount(self) -> None:
        """Withdrawal should have negative amount in cents."""
        result = map_transaction(
            AB_TRANSACTION_WITHDRAWAL, ab_account_name="AB Checking"
        )
        assert result["amount"] < 0

    def test_map_csv_row_format(self) -> None:
        """CSV row should have the expected columns."""
        from finance_sync.exporter.actual_budget.transaction_mapper import (
            map_transaction_to_csv_row,
        )

        row = map_transaction_to_csv_row(AB_TRANSACTION_PAYMENT)
        assert "Date" in row
        assert "Payee" in row
        assert "Category" in row
        assert "Notes" in row
        assert "Amount" in row

    def test_map_csv_row_amount(self) -> None:
        """CSV amount should be a decimal string."""
        from finance_sync.exporter.actual_budget.transaction_mapper import (
            map_transaction_to_csv_row,
        )

        row = map_transaction_to_csv_row(AB_TRANSACTION_DEPOSIT)
        assert row["Amount"] == "1500.00"
        assert row["Payee"] == "Salary deposit"


# ═══════════════════════════════════════════════════════════════════════
# Lifeycle contract
# ═══════════════════════════════════════════════════════════════════════


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
        acct = {"id": str(uuid4()), "name": name, "offbudget": off_budget}
        self._accounts[name] = acct
        return acct

    async def get_or_create_account(
        self, name: str, *, off_budget=False
    ) -> dict:
        existing = await self.get_account_by_name(name)
        if existing:
            return existing
        return await self.create_account(name, off_budget=off_budget)

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
def ab_exporter(ab_config) -> ActualBudgetExporter:
    """Exporter with fully mocked session factory."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.merge = AsyncMock()

    session_factory = MagicMock(return_value=mock_session)

    return ActualBudgetExporter(
        session_factory=session_factory,
        ab_config=ab_config,
        tenant_id="tenant_ab_contract",
    )


@pytest.fixture
def since_time() -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def mock_accounts() -> list[MagicMock]:
    return [AB_ACCOUNT_CHECKING, AB_ACCOUNT_SAVINGS]


@pytest.fixture
def mock_transactions() -> list[MagicMock]:
    return [
        AB_TRANSACTION_PAYMENT,
        AB_TRANSACTION_DEPOSIT,
    ]


@pytest.fixture
def run_ab_export(ab_exporter, since_time):
    """Return a callable that runs AB export with mocked internals.

    The callable accepts ``since``, ``accounts``, ``transactions``,
    ``account_ids``, and ``max_transactions`` keyword arguments.
    """

    async def _run(
        *,
        since=None,
        accounts=None,
        transactions=None,
        account_ids=None,
        max_transactions=None,
    ):
        _since = since or since_time
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        patch_targets = [
            patch.object(ab_exporter, "_last_export_time", return_value=_since),
            patch.object(
                ab_exporter, "_load_accounts", return_value=accounts or []
            ),
            patch.object(
                ab_exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Account"},
            ),
            patch.object(
                ab_exporter,
                "_fetch_pending_transactions",
                return_value=transactions or [],
            ),
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(
                ab_exporter, "_update_export_delivery", return_value=None
            ),
            patch.object(ab_exporter, "_mark_exported", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ]

        with _MultiPatch(*patch_targets):
            return await ab_exporter.run_export(
                since=_since,
                account_ids=account_ids,
                max_transactions=max_transactions,
            )

    return _run


class _MultiPatch:
    """Context manager that enters multiple patches at once."""

    def __init__(self, *patchers):
        self._patchers = patchers

    def __enter__(self):
        for p in self._patchers:
            p.__enter__()
        return self

    def __exit__(self, *args):
        for p in reversed(self._patchers):
            p.__exit__(*args)


class TestActualBudgetLifecycle(ExportLifecycleContractTest):
    """End-to-end export lifecycle with mocked internals."""

    @pytest.fixture
    def run_export_fn(self, run_ab_export):
        return run_ab_export

    # ── Additional AB-specific lifecycle tests ──────────────────────

    @pytest.mark.asyncio
    async def test_run_export_with_transactions(
        self, ab_exporter, since_time
    ) -> None:
        """Transactions should be mapped and imported into AB."""
        from unittest.mock import MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = str(uuid4())
        mock_acct = AB_ACCOUNT_CHECKING
        mock_txns = [
            AB_TRANSACTION_PAYMENT,
            AB_TRANSACTION_DEPOSIT,
        ]

        with (
            patch.object(
                ab_exporter, "_last_export_time", return_value=since_time
            ),
            patch.object(
                ab_exporter, "_load_accounts", return_value=[mock_acct]
            ),
            patch.object(
                ab_exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                ab_exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(
                ab_exporter, "_update_export_delivery", return_value=None
            ),
            patch.object(ab_exporter, "_mark_exported", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await ab_exporter.run_export(since=since_time)

        assert result.status == "completed"
        assert result.transactions_attempted == 2
        assert result.transactions_exported == 2
        assert result.accounts_mapped >= 1

    @pytest.mark.asyncio
    async def test_run_export_connection_failure(
        self, ab_exporter, since_time
    ) -> None:
        """Connection failure results in a failed export."""
        from unittest.mock import MagicMock, patch

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
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                FailingMockClient,
            ),
        ):
            result = await ab_exporter.run_export(since=since_time)

        assert result.status == "failed"
        assert result.error_message is not None
        assert "Connection refused" in result.error_message

    @pytest.mark.asyncio
    async def test_run_export_filtered_account_ids(
        self, ab_exporter, since_time
    ) -> None:
        """Export respects account_ids filter."""
        from unittest.mock import MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        acct_a = AB_ACCOUNT_CHECKING

        with (
            patch.object(
                ab_exporter, "_last_export_time", return_value=since_time
            ),
            patch.object(ab_exporter, "_load_accounts", return_value=[acct_a]),
            patch.object(
                ab_exporter,
                "_fetch_pending_transactions",
                return_value=[AB_TRANSACTION_PAYMENT],
            ),
            patch.object(
                ab_exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(
                ab_exporter, "_update_export_delivery", return_value=None
            ),
            patch.object(ab_exporter, "_mark_exported", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await ab_exporter.run_export(
                since=since_time,
                account_ids=[acct_a.id],
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 1

    @pytest.mark.asyncio
    async def test_max_transactions_limit(
        self, ab_exporter, since_time
    ) -> None:
        """max_transactions should limit the export batch."""
        from unittest.mock import MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        mock_acct = AB_ACCOUNT_CHECKING
        mock_txns = [AB_TRANSACTION_PAYMENT for _ in range(5)]

        with (
            patch.object(
                ab_exporter, "_last_export_time", return_value=since_time
            ),
            patch.object(
                ab_exporter, "_load_accounts", return_value=[mock_acct]
            ),
            patch.object(
                ab_exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                ab_exporter,
                "_resolve_ab_account",
                return_value={"id": str(uuid4()), "name": "AB Checking"},
            ),
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(
                ab_exporter, "_update_export_delivery", return_value=None
            ),
            patch.object(ab_exporter, "_mark_exported", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await ab_exporter.run_export(
                since=since_time,
                max_transactions=2,
            )

        assert result.status == "completed"
        # Attempted counts transactions after the limit is applied
        assert result.transactions_attempted == 2  # limited by max
        assert result.transactions_exported == 2  # limited by max

    @pytest.mark.asyncio
    async def test_run_export_no_transactions(
        self, ab_exporter, since_time
    ) -> None:
        """No transactions should return completed result with zero counts."""
        from unittest.mock import MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                ab_exporter, "_last_export_time", return_value=since_time
            ),
            patch.object(ab_exporter, "_load_accounts", return_value=[]),
            patch.object(ab_exporter, "_write_csv", return_value=None),
            patch.object(ab_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ExportRun",
                return_value=mock_run,
            ),
            patch(
                "finance_sync.exporter.actual_budget.exporter.ActualBudgetClient",
                MockABClient,
            ),
        ):
            result = await ab_exporter.run_export(since=since_time)

        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0
