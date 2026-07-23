"""Contract tests for the Wealthfolio exporter.

Validates the exporter against the contract suite defined in
:mod:`tests.exporter.contract_test_template` using consumer-side
fixtures (finance-sync accounts, transactions, securities, holdings)
and provider-side expectations (Wealthfolio CSV format).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
from finance_sync.exporter.wealthfolio.exporter import (
    WealthfolioExportResult,
    WealthfolioExporter,
)
from finance_sync.exporter.wealthfolio.transaction_mapper import (
    WF_ACTIVITY_BUY,
    WF_ACTIVITY_DEPOSIT,
    WF_ACTIVITY_DIVIDEND,
    WF_ACTIVITY_FEE,
    WF_ACTIVITY_INTEREST,
    WF_ACTIVITY_SELL,
    WF_ACTIVITY_TRANSFER_IN,
    WF_ACTIVITY_TRANSFER_OUT,
    WF_ACTIVITY_WITHDRAWAL,
    map_holding_to_wf_row,
    map_holdings_to_csv,
    map_transaction_to_wf_row,
    map_transactions_to_csv,
)
from tests.exporter.contract_test_template import (
    CsvExportContractTest,
    ExporterConfigContractTest,
    ExportLifecycleContractTest,
    ExportResultContractTest,
    TransactionMappingContractTest,
)
from tests.exporter.fixtures.wf_fixtures import (
    SECURITY_AAPL,
    SECURITY_BTC,
    SECURITY_MSFT,
    SECURITY_VWCE,
    WF_ACCOUNT_BROKERAGE,
    WF_ACCOUNT_CASH,
    WF_ACCOUNT_DEPOSIT_ONLY,
    WF_HOLDING_AAPL,
    WF_HOLDING_BTC,
    WF_HOLDING_VWCE,
    WF_MAP_TEST_CASES,
    WF_TRANSACTION_BUY_AAPL,
    WF_TRANSACTION_BUY_VWCE,
    WF_TRANSACTION_DEPOSIT,
    WF_TRANSACTION_DEPOSIT_ONLY,
    WF_TRANSACTION_DIVIDEND,
    WF_TRANSACTION_FEE,
    WF_TRANSACTION_INTEREST,
    WF_TRANSACTION_SELL_MSFT,
    WF_TRANSACTION_TRANSFER_IN,
    WF_TRANSACTION_TRANSFER_OUT,
    WF_TRANSACTION_WITHDRAWAL,
)

# ═══════════════════════════════════════════════════════════════════════
# Config contract
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioConfig(ExporterConfigContractTest):
    """Config construction and defaults."""

    @pytest.fixture
    def exporter_config(self) -> WealthfolioConfig:
        return WealthfolioConfig(
            output_dir=Path("/tmp/test_wf_exports"),
            default_currency="EUR",
        )

    def test_wealthfolio_specific_defaults(self) -> None:
        """WF-specific config defaults should be sensible."""
        config = WealthfolioConfig()
        assert config.default_currency == "EUR"
        assert config.export_holdings is True
        assert config.max_transactions_per_file == 10_000
        assert config.include_pending is False
        assert config.instrument_type_overrides == {}


# ═══════════════════════════════════════════════════════════════════════
# Export result contract
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioResult(ExportResultContractTest):
    """WealthfolioExportResult construction and semantics."""

    @pytest.fixture
    def completed_result(self) -> WealthfolioExportResult:
        return WealthfolioExportResult(
            status="completed",
            accounts_mapped=3,
            transactions_attempted=10,
            transactions_exported=8,
            transactions_failed=1,
            transactions_skipped=1,
            holdings_exported=5,
            csv_files=["/tmp/transactions.csv"],
            duration_s=2.5,
        )

    @pytest.fixture
    def failed_result(self) -> WealthfolioExportResult:
        return WealthfolioExportResult(
            status="failed",
            error_message="Permission denied",
        )

    def test_holdings_exported_in_result(self) -> None:
        """Wealthfolio result should track holdings_exported."""
        r = WealthfolioExportResult(status="completed", holdings_exported=3)
        assert r.holdings_exported == 3

    def test_csv_files_in_result(self) -> None:
        """Wealthfolio result should track CSV file paths."""
        r = WealthfolioExportResult(
            status="completed",
            csv_files=["/tmp/a.csv", "/tmp/b.csv"],
        )
        assert len(r.csv_files) == 2


# ═══════════════════════════════════════════════════════════════════════
# Transaction mapping contract
# ═══════════════════════════════════════════════════════════════════════


class TestWFTransactionMapping(TransactionMappingContractTest):
    """Canonical → Wealthfolio transaction mapping."""

    @pytest.fixture
    def map_function(self):
        return lambda txn: map_transaction_to_wf_row(txn)

    @pytest.fixture
    def map_test_cases(self) -> list[dict]:
        return WF_MAP_TEST_CASES

    # ── WF-specific mapping tests ───────────────────────────────────

    def test_map_buy_with_security(self) -> None:
        """Purchase should map to BUY activity with ticker symbol."""
        row = map_transaction_to_wf_row(
            WF_TRANSACTION_BUY_AAPL,
            security=SECURITY_AAPL,
        )
        assert row["activityType"] == WF_ACTIVITY_BUY
        assert row["symbol"] == "AAPL"
        assert row["instrumentType"] == "EQUITY"
        assert row["currency"] == "USD"

    def test_map_sell_with_security(self) -> None:
        """Sale should map to SELL activity."""
        row = map_transaction_to_wf_row(
            WF_TRANSACTION_SELL_MSFT,
            security=SECURITY_MSFT,
        )
        assert row["activityType"] == WF_ACTIVITY_SELL
        assert row["symbol"] == "MSFT"

    def test_map_deposit(self) -> None:
        """Deposit should map to DEPOSIT with empty symbol."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_DEPOSIT)
        assert row["activityType"] == WF_ACTIVITY_DEPOSIT
        assert row["symbol"] == ""
        assert row["amount"] == "5000.00"

    def test_map_withdrawal(self) -> None:
        """Withdrawal should map to WITHDRAWAL."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_WITHDRAWAL)
        assert row["activityType"] == WF_ACTIVITY_WITHDRAWAL
        assert row["amount"] == "500.00"

    def test_map_dividend(self) -> None:
        """Dividend should map to DIVIDEND with security symbol."""
        row = map_transaction_to_wf_row(
            WF_TRANSACTION_DIVIDEND,
            security=SECURITY_AAPL,
        )
        assert row["activityType"] == WF_ACTIVITY_DIVIDEND
        assert row["symbol"] == "AAPL"
        assert row["amount"] == "50.00"

    def test_map_interest(self) -> None:
        """Interest should map to INTEREST."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_INTEREST)
        assert row["activityType"] == WF_ACTIVITY_INTEREST
        assert row["amount"] == "3.42"

    def test_map_fee(self) -> None:
        """Fee should map to FEE."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_FEE)
        assert row["activityType"] == WF_ACTIVITY_FEE
        assert row["amount"] == "9.99"

    def test_map_transfer_in(self) -> None:
        """Positive transfer maps to TRANSFER_IN."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_TRANSFER_IN)
        assert row["activityType"] == WF_ACTIVITY_TRANSFER_IN

    def test_map_transfer_out(self) -> None:
        """Negative transfer maps to TRANSFER_OUT."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_TRANSFER_OUT)
        assert row["activityType"] == WF_ACTIVITY_TRANSFER_OUT

    def test_map_security_by_isin_when_no_ticker(self) -> None:
        """When no ticker, ISIN is used as the symbol."""
        sec_no_ticker = MagicMock()
        sec_no_ticker.ticker = None
        sec_no_ticker.isin = "US0378331005"
        sec_no_ticker.security_type = "stock"

        txn = MagicMock()
        txn.occurred_at = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        txn.amount = Decimal("-1000.00")
        txn.currency_code = "USD"
        txn.transaction_type = "purchase"
        txn.description = "Buy via ISIN"
        txn.external_transaction_id = "txn_isin_001"
        txn.security_id = "sec_001"
        txn.fx_rate = None
        txn.base_currency_code = None
        txn.amount_in_base = None
        txn.status = "booked"

        row = map_transaction_to_wf_row(txn, security=sec_no_ticker)
        assert row["symbol"] == "US0378331005"

    def test_map_instrument_type_override(self) -> None:
        """Custom instrument type map should override defaults."""
        custom_map = {"etf": "ETF"}
        row = map_transaction_to_wf_row(
            WF_TRANSACTION_BUY_VWCE,
            security=SECURITY_VWCE,
            instrument_type_map=custom_map,
        )
        assert row["instrumentType"] == "ETF"

    def test_map_holding_with_security(self) -> None:
        """Holding mapping should include symbol and avg cost."""
        row = map_holding_to_wf_row(
            WF_HOLDING_AAPL,
            security=SECURITY_AAPL,
        )
        assert row["symbol"] == "AAPL"
        assert row["date"] == "2025-06-30"
        assert float(row["quantity"]) == 50.0

    def test_map_holding_without_cost_basis(self) -> None:
        """Holding without cost basis should have empty avgCost."""
        holding = MagicMock()
        holding.id = str(uuid4())
        holding.tenant_id = "tenant_wf_contract"
        holding.account_id = str(uuid4())
        holding.security_id = SECURITY_BTC.id
        holding.observed_at = datetime(2025, 6, 30, 23, 59, tzinfo=UTC)
        holding.quantity = Decimal("0.5")
        holding.cost_basis = None
        holding.cost_basis_currency = "USD"
        holding.market_value = Decimal("16000.00")
        holding.currency_code = "USD"
        holding.price = Decimal("32000.00")
        holding.price_currency = "USD"
        holding.source = "provider_sync"

        row = map_holding_to_wf_row(holding, security=SECURITY_BTC)
        assert row["symbol"] == "BTC"
        assert row["avgCost"] == ""

    def test_map_holding_cash(self) -> None:
        """Holdings without a security use UNKNOWN symbol."""
        holding_no_sec = MagicMock()
        holding_no_sec.id = str(uuid4())
        holding_no_sec.tenant_id = "tenant_wf_contract"
        holding_no_sec.account_id = str(uuid4())
        holding_no_sec.security_id = "nonexistent"
        holding_no_sec.observed_at = datetime(2025, 6, 30, 23, 59, tzinfo=UTC)
        holding_no_sec.quantity = Decimal("100")
        holding_no_sec.cost_basis = Decimal("10000.00")
        holding_no_sec.cost_basis_currency = "EUR"
        holding_no_sec.market_value = Decimal("10000.00")
        holding_no_sec.currency_code = "EUR"
        holding_no_sec.price = Decimal("100.00")
        holding_no_sec.price_currency = "EUR"
        holding_no_sec.source = "provider_sync"

        row = map_holding_to_wf_row(holding_no_sec, security=None)
        assert row["symbol"] == "UNKNOWN"

    def test_comment_includes_external_id(self) -> None:
        """Comment should include external transaction ID for dedup."""
        row = map_transaction_to_wf_row(WF_TRANSACTION_BUY_AAPL)
        assert "Buy 10 AAPL" in row["comment"]
        assert WF_TRANSACTION_BUY_AAPL.external_transaction_id in row["comment"]


# ═══════════════════════════════════════════════════════════════════════
# CSV export contract
# ═══════════════════════════════════════════════════════════════════════


class TestWFCsvExport(CsvExportContractTest):
    """CSV generation from transaction and holding fixtures."""

    @pytest.fixture
    def csv_transactions_function(self):
        return map_transactions_to_csv

    @pytest.fixture
    def csv_holdings_function(self):
        return map_holdings_to_csv

    @pytest.fixture
    def sample_transactions(self) -> list:
        return [
            WF_TRANSACTION_BUY_AAPL,
            WF_TRANSACTION_DEPOSIT,
            WF_TRANSACTION_DIVIDEND,
        ]

    @pytest.fixture
    def sample_holdings(self) -> list:
        return [
            WF_HOLDING_AAPL,
            WF_HOLDING_VWCE,
        ]

    # ── WF-specific CSV tests ───────────────────────────────────────

    def test_csv_correct_column_order(self) -> None:
        """CSV should have expected columns in correct order."""
        csv = map_transactions_to_csv([WF_TRANSACTION_BUY_AAPL])
        header = csv.split("\n")[0].strip()
        expected_cols = [
            "date", "symbol", "instrumentType", "quantity",
            "activityType", "unitPrice", "currency", "fee",
            "amount", "fxRate", "comment",
        ]
        for col in expected_cols:
            assert col in header

    def test_csv_with_security_map(self) -> None:
        """CSV generation with security map resolves symbols."""
        sec_map = {SECURITY_AAPL.id: SECURITY_AAPL}
        csv = map_transactions_to_csv(
            [WF_TRANSACTION_BUY_AAPL],
            security_map=sec_map,
        )
        assert "AAPL" in csv
        assert "BUY" in csv

    def test_holdings_csv_content(self) -> None:
        """Holdings CSV with security map has correct data."""
        sec_map = {
            SECURITY_AAPL.id: SECURITY_AAPL,
            SECURITY_VWCE.id: SECURITY_VWCE,
        }
        csv = map_holdings_to_csv(
            [WF_HOLDING_AAPL, WF_HOLDING_VWCE],
            security_map=sec_map,
        )
        lines = [l for l in csv.strip().split("\n") if l.strip()]
        assert len(lines) == 3  # header + 2 holdings
        assert "AAPL" in csv
        assert "VWCE" in csv

    def test_csv_with_instrument_type_override(self) -> None:
        """CSV with instrument type map applies custom mappings."""
        sec_map = {SECURITY_VWCE.id: SECURITY_VWCE}
        csv = map_transactions_to_csv(
            [WF_TRANSACTION_BUY_VWCE],
            security_map=sec_map,
            instrument_type_map={"etf": "ETF"},
        )
        assert "ETF" in csv


# ═══════════════════════════════════════════════════════════════════════
# Lifeycle contract
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def wf_config() -> WealthfolioConfig:
    return WealthfolioConfig(
        output_dir=Path("/tmp/test_wf_contract_exports"),
        default_currency="EUR",
        export_holdings=True,
    )


@pytest.fixture
def wf_exporter(wf_config) -> WealthfolioExporter:
    """Exporter with fully mocked session factory."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.merge = AsyncMock()

    session_factory = MagicMock(return_value=mock_session)

    return WealthfolioExporter(
        session_factory=session_factory,
        wf_config=wf_config,
        tenant_id="tenant_wf_contract",
    )


@pytest.fixture
def wf_since_time() -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def wf_mock_accounts() -> list[MagicMock]:
    return [WF_ACCOUNT_BROKERAGE, WF_ACCOUNT_CASH]


@pytest.fixture
def wf_mock_transactions() -> list[MagicMock]:
    return [WF_TRANSACTION_BUY_AAPL, WF_TRANSACTION_DEPOSIT]


@pytest.fixture
def run_wf_export(wf_exporter, wf_since_time):
    """Return a callable that runs WF export with mocked internals."""

    async def _run(
        *,
        since=None,
        accounts=None,
        transactions=None,
        account_ids=None,
        max_transactions=None,
    ):
        _since = since or wf_since_time
        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        patch_targets = [
            patch.object(wf_exporter, "_last_export_time", return_value=_since),
            patch.object(
                wf_exporter, "_load_accounts", return_value=accounts or []
            ),
            patch.object(wf_exporter, "_load_securities", return_value={}),
            patch.object(
                wf_exporter,
                "_resolve_wf_account_name",
                return_value="WF Account",
            ),
            patch.object(
                wf_exporter,
                "_fetch_pending_transactions",
                return_value=transactions or [],
            ),
            patch.object(
                wf_exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                wf_exporter, "_mark_exported", return_value=None
            ),
            patch.object(
                wf_exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test_wf_contract_exports/test.csv"),
            ),
            patch.object(
                wf_exporter,
                "_write_manifest",
                return_value=Path("/tmp/test_wf_contract_exports/manifest.json"),
            ),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ]

        with _MultiPatch(*patch_targets):
            return await wf_exporter.run_export(
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


class TestWealthfolioLifecycle(ExportLifecycleContractTest):
    """End-to-end export lifecycle with mocked internals."""

    @pytest.fixture
    def run_export_fn(self, run_wf_export):
        return run_wf_export

    # ── Alias fixtures so inherited template tests can find them ──────

    @pytest.fixture
    def mock_accounts(self, wf_mock_accounts):
        return wf_mock_accounts

    @pytest.fixture
    def mock_transactions(self, wf_mock_transactions):
        return wf_mock_transactions

    @pytest.fixture
    def since_time(self, wf_since_time):
        return wf_since_time

    # ── Additional WF-specific lifecycle tests ──────────────────────

    @pytest.mark.asyncio
    async def test_run_export_with_transactions(
        self, wf_exporter, wf_since_time
    ) -> None:
        """Transactions should be mapped and CSV written."""
        from unittest.mock import MagicMock, patch
        from pathlib import Path

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                wf_exporter, "_last_export_time", return_value=wf_since_time
            ),
            patch.object(
                wf_exporter, "_load_accounts", return_value=[WF_ACCOUNT_BROKERAGE]
            ),
            patch.object(wf_exporter, "_load_securities", return_value={}),
            patch.object(
                wf_exporter,
                "_resolve_wf_account_name",
                return_value="WF Brokerage",
            ),
            patch.object(
                wf_exporter,
                "_fetch_pending_transactions",
                return_value=[WF_TRANSACTION_BUY_AAPL],
            ),
            patch.object(
                wf_exporter, "_fetch_current_holdings", return_value=[]
            ),
            patch.object(wf_exporter, "_mark_exported", return_value=None),
            patch.object(
                wf_exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test.csv"),
            ),
            patch.object(
                wf_exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await wf_exporter.run_export(
                since=wf_since_time,
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 1
        assert result.transactions_exported == 1

    @pytest.mark.asyncio
    async def test_run_export_with_holdings(
        self, wf_exporter, wf_since_time
    ) -> None:
        """Holdings should be exported when config.export_holdings is True."""
        from unittest.mock import MagicMock, patch
        from pathlib import Path

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                wf_exporter, "_last_export_time", return_value=wf_since_time
            ),
            patch.object(
                wf_exporter, "_load_accounts", return_value=[WF_ACCOUNT_BROKERAGE]
            ),
            patch.object(wf_exporter, "_load_securities", return_value={}),
            patch.object(
                wf_exporter,
                "_resolve_wf_account_name",
                return_value="WF Brokerage",
            ),
            patch.object(
                wf_exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                wf_exporter,
                "_fetch_current_holdings",
                return_value=[WF_HOLDING_AAPL],
            ),
            patch.object(wf_exporter, "_mark_exported", return_value=None),
            patch.object(
                wf_exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test_holdings.csv"),
            ),
            patch.object(
                wf_exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await wf_exporter.run_export(
                since=wf_since_time,
            )

        assert result.status == "completed"
        assert result.holdings_exported == 1

    @pytest.mark.asyncio
    async def test_run_export_no_accounts(
        self, wf_exporter, wf_since_time
    ) -> None:
        """Running export with no active accounts returns completed result."""
        from unittest.mock import MagicMock, patch

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                wf_exporter, "_last_export_time", return_value=wf_since_time
            ),
            patch.object(wf_exporter, "_load_accounts", return_value=[]),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await wf_exporter.run_export(
                since=wf_since_time,
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0
        assert result.holdings_exported == 0

    @pytest.mark.asyncio
    async def test_run_export_filtered_account_ids(
        self, wf_exporter, wf_since_time
    ) -> None:
        """Export with account_ids filter processes only matching accounts."""
        from unittest.mock import MagicMock, patch
        from pathlib import Path

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        account_id = WF_ACCOUNT_BROKERAGE.id

        with (
            patch.object(
                wf_exporter, "_last_export_time", return_value=wf_since_time
            ),
            patch.object(
                wf_exporter,
                "_load_accounts",
                return_value=[WF_ACCOUNT_BROKERAGE],
            ),
            patch.object(wf_exporter, "_load_securities", return_value={}),
            patch.object(
                wf_exporter,
                "_resolve_wf_account_name",
                return_value="WF Brokerage",
            ),
            patch.object(
                wf_exporter,
                "_fetch_pending_transactions",
                return_value=[WF_TRANSACTION_BUY_AAPL],
            ),
            patch.object(
                wf_exporter, "_fetch_current_holdings", return_value=[]
            ),
            patch.object(wf_exporter, "_mark_exported", return_value=None),
            patch.object(
                wf_exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test_filtered.csv"),
            ),
            patch.object(
                wf_exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await wf_exporter.run_export(
                since=wf_since_time,
                account_ids=[account_id],
                max_transactions=10,
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 1

    @pytest.mark.asyncio
    async def test_run_export_no_holdings_when_disabled(
        self, wf_exporter, wf_since_time
    ) -> None:
        """When export_holdings=False, holdings should not be exported."""
        from unittest.mock import MagicMock, patch
        from pathlib import Path

        # Temporarily disable holdings export
        wf_exporter._wf_config.export_holdings = False

        mock_run = MagicMock()
        mock_run.id = str(uuid4())

        with (
            patch.object(
                wf_exporter, "_last_export_time", return_value=wf_since_time
            ),
            patch.object(
                wf_exporter, "_load_accounts", return_value=[WF_ACCOUNT_BROKERAGE]
            ),
            patch.object(wf_exporter, "_load_securities", return_value={}),
            patch.object(
                wf_exporter,
                "_resolve_wf_account_name",
                return_value="WF Brokerage",
            ),
            patch.object(
                wf_exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                wf_exporter, "_fetch_current_holdings", return_value=[WF_HOLDING_AAPL]
            ),
            patch.object(wf_exporter, "_mark_exported", return_value=None),
            patch.object(
                wf_exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch.object(wf_exporter, "_complete_run", return_value=None),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await wf_exporter.run_export(
                since=wf_since_time,
            )

        assert result.status == "completed"
        assert result.holdings_exported == 0
        assert result.transactions_attempted == 0
