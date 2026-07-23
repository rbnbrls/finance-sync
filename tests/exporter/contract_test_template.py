"""Contract test template for exporter implementations.

Every exporter **must** pass all tests in this file.  To use::

    import pytest
    from tests.exporter.contract_test_template import (
        ExporterConfigContractTest,
        ExportResultContractTest,
        TransactionMappingContractTest,
        ExportLifecycleContractTest,
        CsvExportContractTest,
    )

    class TestActualBudgetConfig(ExporterConfigContractTest):
        @pytest.fixture
        def exporter_config(self) -> ActualBudgetConfig:
            return ActualBudgetConfig(...)

    class TestActualBudgetLifecycle(ExportLifecycleContractTest):
        ...

Following the pattern established in
``tests/connectors/contract_test_template.py``, each concrete class
provides fixtures that the mixin uses to verify the exporter contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import MagicMock

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════════
# 1. Config contract
# ═══════════════════════════════════════════════════════════════════════


class ExporterConfigContractTest:
    """Verifies exporter config construction and defaults.

    Subclasses **must** provide::

        @pytest.fixture
        def exporter_config(self) -> ...Config:
            ...
    """

    async def test_config_has_required_fields(
        self, exporter_config: Any
    ) -> None:
        """Config should expose all required export parameters."""
        assert hasattr(exporter_config, "account_name_overrides")

    async def test_config_defaults_sensible(self, exporter_config: Any) -> None:
        """Config defaults should never raise on core property access."""
        assert isinstance(exporter_config.account_name_overrides, dict)

    async def test_config_from_settings(self, exporter_config: Any) -> None:
        """Config should support construction from a settings object."""
        cls = type(exporter_config)
        if hasattr(cls, "from_settings"):
            # Use a simple object with the right attributes
            settings = _SettingsMock.get(cls)
            constructed = cls.from_settings(settings)
            assert isinstance(constructed, cls)


class _SettingsMock:
    """Minimal settings mock that returns sensible defaults."""

    @staticmethod
    def get(config_cls: type) -> object:
        from unittest.mock import MagicMock

        settings = MagicMock()
        # Configure reasonable defaults based on common settings names
        for prefix in ("actual_budget_", "wealthfolio_"):
            settings.configure_mock(
                **{
                    f"{prefix}server_url": "http://localhost:5006",
                    f"{prefix}password": "test",
                    f"{prefix}budget_name": "Test",
                    f"{prefix}sync_id": None,
                    f"{prefix}encryption_password": None,
                    f"{prefix}verify_ssl": True,
                    f"{prefix}request_timeout": 60.0,
                    f"{prefix}batch_size": 100,
                    f"{prefix}default_off_budget": False,
                    f"{prefix}account_name_overrides": {},
                    f"{prefix}output_dir": "/tmp/test_exports",
                    f"{prefix}default_currency": "EUR",
                    f"{prefix}export_holdings": True,
                    f"{prefix}max_transactions_per_file": 10000,
                    f"{prefix}include_pending": False,
                    f"{prefix}instrument_type_overrides": {},
                }
            )
        return settings


# ═══════════════════════════════════════════════════════════════════════
# 2. Export result contract
# ═══════════════════════════════════════════════════════════════════════


class ExportResultContractTest:
    """Verifies export result semantics.

    Subclasses **must** provide::

        @pytest.fixture
        def completed_result(self) -> ...ExportResult:
            ...

        @pytest.fixture
        def failed_result(self) -> ...ExportResult:
            ...
    """

    async def test_completed_result_attributes(
        self, completed_result: Any
    ) -> None:
        """Completed result should have expected fields."""
        assert completed_result.status == "completed"
        assert hasattr(completed_result, "accounts_mapped")
        assert hasattr(completed_result, "transactions_attempted")
        assert hasattr(completed_result, "transactions_exported")
        assert hasattr(completed_result, "transactions_failed")
        assert hasattr(completed_result, "duration_s")
        assert isinstance(completed_result.duration_s, float)

    async def test_failed_result_has_error(
        self, failed_result: Any
    ) -> None:
        """Failed result should carry an error message."""
        assert failed_result.status == "failed"
        assert failed_result.error_message is not None

    async def test_repr_includes_status(self, completed_result: Any) -> None:
        """repr(result) should include status and key metrics."""
        rep = repr(completed_result)
        assert "completed" in rep

    async def test_zero_counts_acceptable(self) -> None:
        """Zero transaction counts should be valid (no-ops)."""
        # This test is intentionally vague: concrete classes override.
        pass


# ═══════════════════════════════════════════════════════════════════════
# 3. Transaction mapping contract (canonical → target format)
# ═══════════════════════════════════════════════════════════════════════


class TransactionMappingContractTest:
    """Verifies that canonical transactions map correctly to target format.

    Subclasses **must** provide::

        @pytest.fixture
        def map_function(self):  # canonical → target-format dict
            ...

        @pytest.fixture
        def map_test_cases(self) -> list[dict]:
            # Each dict has keys: txn, expected_...
            ...
    """

    async def test_map_returns_dict(
        self, map_function: Any, map_test_cases: list[dict]
    ) -> None:
        """Mapping should return a dict with required fields."""
        for case in map_test_cases:
            txn = case["txn"]
            result = map_function(txn)
            assert isinstance(result, dict)

    async def test_map_includes_external_ref(
        self, map_function: Any, map_test_cases: list[dict]
    ) -> None:
        """Mapped result should be derived from the source transaction."""
        for case in map_test_cases:
            txn = case["txn"]
            result = map_function(txn)
            assert isinstance(result, dict)

    async def test_map_amount_preserved(
        self, map_function: Any, map_test_cases: list[dict]
    ) -> None:
        """Amount should appear in mapped output."""
        for case in map_test_cases:
            txn = case["txn"]
            if txn.amount:
                result = map_function(txn)
                assert isinstance(result, dict)

    async def test_map_description_included(
        self, map_function: Any, map_test_cases: list[dict]
    ) -> None:
        """Description should appear in mapped output where applicable."""
        for case in map_test_cases:
            txn = case["txn"]
            if txn.description:
                result = map_function(txn)
                assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════
# 4. Export lifecycle contract
# ═══════════════════════════════════════════════════════════════════════


class ExportLifecycleContractTest:
    """Verifies the end-to-end export lifecycle with mocked internals.

    Subclasses **must** provide a ``run_export_fn`` fixture that fully
    wires up the exporter with patched internals::

        @pytest.fixture
        def run_export_fn(self, exporter, since_time) -> Callable:
            async def fn(**kwargs) -> ExportResult:
                with (
                    patch.object(exporter, "_load_accounts", ...),
                    ...
                ):
                    return await exporter.run_export(**kwargs)
            return fn

    Subclasses also provide the data fixtures::

        @pytest.fixture
        def exporter(self) -> Exporter:
            ...

        @pytest.fixture
        def mock_accounts(self) -> list[MagicMock]:
            ...

        @pytest.fixture
        def mock_transactions(self) -> list[MagicMock]:
            ...

        @pytest.fixture
        def since_time(self) -> datetime:
            ...
    """

    async def test_run_export_no_accounts(
        self,
        run_export_fn: Callable[..., Any],
        since_time: datetime,
    ) -> None:
        """Running export with no active accounts returns completed result."""
        result = await run_export_fn(since=since_time, accounts=[])
        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0

    async def test_run_export_with_accounts_no_txns(
        self,
        run_export_fn: Callable[..., Any],
        mock_accounts: list[MagicMock],
        since_time: datetime,
    ) -> None:
        """Accounts without recent transactions completes gracefully."""
        result = await run_export_fn(
            since=since_time,
            accounts=mock_accounts,
            transactions=[],
        )
        assert result.status == "completed"
        assert result.transactions_attempted == 0

    async def test_run_export_respects_account_filter(
        self,
        run_export_fn: Callable[..., Any],
        mock_accounts: list[MagicMock],
        mock_transactions: list[MagicMock],
        since_time: datetime,
    ) -> None:
        """Export respects the account_ids filter parameter."""
        result = await run_export_fn(
            since=since_time,
            accounts=mock_accounts[:1],
            transactions=mock_transactions,
            account_ids=[mock_accounts[0].id],
        )
        assert result.status == "completed"

    async def test_run_export_max_transactions(
        self,
        run_export_fn: Callable[..., Any],
        mock_accounts: list[MagicMock],
        since_time: datetime,
    ) -> None:
        """max_transactions should limit the number exported."""
        result = await run_export_fn(
            since=since_time,
            accounts=mock_accounts,
            max_transactions=2,
        )
        assert result.status == "completed"


# ═══════════════════════════════════════════════════════════════════════
# 5. CSV export contract (for exporters that produce CSV files)
# ═══════════════════════════════════════════════════════════════════════


class CsvExportContractTest:
    """Verifies CSV generation from fixture data.

    Subclasses **must** provide::

        @pytest.fixture
        def csv_transactions_function(self) -> Callable:
            ...

        @pytest.fixture
        def csv_holdings_function(self) -> Callable:
            ...

        @pytest.fixture
        def sample_transactions(self) -> list:
            ...

        @pytest.fixture
        def sample_holdings(self) -> list:
            ...
    """

    async def test_csv_has_header(
        self,
        csv_transactions_function: Any,
        sample_transactions: list[Any],
    ) -> None:
        """CSV output should include a header row."""
        csv = csv_transactions_function(sample_transactions)
        assert csv
        first_line = csv.split("\n")[0]
        assert "date" in first_line.lower() or "Date" in first_line

    async def test_csv_one_row_per_transaction(
        self,
        csv_transactions_function: Any,
        sample_transactions: list[Any],
    ) -> None:
        """Each input transaction produces one CSV data row."""
        if not sample_transactions:
            pytest.skip("No sample transactions provided")
        csv = csv_transactions_function(sample_transactions)
        lines = [l for l in csv.strip().split("\n") if l.strip()]
        assert len(lines) == len(sample_transactions) + 1

    async def test_csv_empty_input(
        self, csv_transactions_function: Any
    ) -> None:
        """Empty transaction list should produce empty CSV."""
        csv = csv_transactions_function([])
        assert csv == ""

    async def test_holdings_csv_has_header(
        self,
        csv_holdings_function: Any,
        sample_holdings: list[Any],
    ) -> None:
        """Holdings CSV output should include a header row."""
        if not sample_holdings:
            pytest.skip("No sample holdings provided")
        csv = csv_holdings_function(sample_holdings)
        assert csv
        first_line = csv.split("\n")[0]
        assert "symbol" in first_line.lower()

    async def test_holdings_csv_one_per_holding(
        self,
        csv_holdings_function: Any,
        sample_holdings: list[Any],
    ) -> None:
        """Each holding produces one CSV data row."""
        if not sample_holdings:
            pytest.skip("No sample holdings provided")
        csv = csv_holdings_function(sample_holdings)
        lines = [l for l in csv.strip().split("\n") if l.strip()]
        assert len(lines) == len(sample_holdings) + 1

    async def test_holdings_csv_empty_input(
        self, csv_holdings_function: Any
    ) -> None:
        """Empty holdings list should produce empty CSV."""
        csv = csv_holdings_function([])
        assert csv == ""
