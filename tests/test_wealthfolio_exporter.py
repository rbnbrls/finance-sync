"""Tests for the Wealthfolio exporter service and transaction mapper.

Uses mock DB sessions and realistic fixture data.
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
    WealthfolioExporter,
    WealthfolioExportResult,
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

# ═══════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_mock_account(**kwargs):
    """Build a mock Account ORM instance."""
    acct = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "tenant_id": "tenant_001",
        "provider_key": "trading212",
        "external_account_id": "ext_acct_001",
        "name": "Brokerage Account",
        "account_type": "brokerage",
        "currency_code": "EUR",
        "is_active": True,
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(acct, k, v)
    return acct


def _make_mock_transaction(**kwargs):
    """Build a mock Transaction ORM instance."""
    txn = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "tenant_id": "tenant_001",
        "account_id": "acct_001",
        "security_id": None,
        "provider_key": "trading212",
        "external_transaction_id": f"ext_{uuid4().hex[:8]}",
        "amount": Decimal("-42.50"),
        "currency_code": "EUR",
        "amount_in_base": None,
        "base_currency_code": None,
        "fx_rate": None,
        "occurred_at": datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
        "booked_at": datetime(2025, 6, 15, 14, 0, tzinfo=UTC),
        "transaction_type": "payment",
        "description": "Coffee Shop",
        "status": "booked",
        "revision": 1,
        "provider_fingerprint": None,
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(txn, k, v)
    return txn


def _make_mock_security(**kwargs):
    """Build a mock Security ORM instance."""
    sec = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "isin": "US0378331005",
        "figi": "BBG000B9XRY4",
        "cusip": "037833100",
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "security_type": "stock",
        "currency_code": "USD",
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(sec, k, v)
    return sec


def _make_mock_holding(**kwargs):
    """Build a mock Holding ORM instance."""
    holding = MagicMock()
    defaults = {
        "id": str(uuid4()),
        "tenant_id": "tenant_001",
        "account_id": "acct_001",
        "security_id": str(uuid4()),
        "observed_at": datetime(2025, 6, 30, 23, 59, tzinfo=UTC),
        "quantity": Decimal(50),
        "cost_basis": Decimal("8574.00"),
        "cost_basis_currency": "USD",
        "market_value": Decimal("9500.00"),
        "currency_code": "USD",
        "price": Decimal("190.00"),
        "price_currency": "USD",
        "source": "provider_sync",
    }
    for k, v in {**defaults, **kwargs}.items():
        setattr(holding, k, v)
    return holding


# ═══════════════════════════════════════════════════════════════════════
# Tests for WealthfolioConfig
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioConfig:
    def test_default_config(self) -> None:
        config = WealthfolioConfig()
        assert config.default_currency == "EUR"
        assert config.export_holdings is True
        assert config.max_transactions_per_file == 10_000
        assert config.include_pending is False
        assert config.account_name_overrides == {}
        assert config.instrument_type_overrides == {}

    def test_from_settings(self) -> None:
        settings = MagicMock()
        settings.wealthfolio_output_dir = "/custom/path"
        settings.wealthfolio_default_currency = "USD"
        settings.wealthfolio_export_holdings = False
        settings.wealthfolio_max_transactions_per_file = 5000
        settings.wealthfolio_include_pending = True
        settings.wealthfolio_account_name_overrides = {"acct_1": "WF Broker"}
        settings.wealthfolio_instrument_type_overrides = {"crypto": "CRYPTO"}

        config = WealthfolioConfig.from_settings(settings)
        assert str(config.output_dir) == "/custom/path"
        assert config.default_currency == "USD"
        assert config.export_holdings is False
        assert config.max_transactions_per_file == 5000
        assert config.include_pending is True
        assert config.account_name_overrides == {"acct_1": "WF Broker"}
        assert config.instrument_type_overrides == {"crypto": "CRYPTO"}


# ═══════════════════════════════════════════════════════════════════════
# Tests for transaction mapper
# ═══════════════════════════════════════════════════════════════════════


class TestTransactionMapper:
    def test_map_purchase_with_security(self) -> None:
        sec = _make_mock_security()
        txn = _make_mock_transaction(
            transaction_type="purchase",
            amount=Decimal("-1505.00"),
            currency_code="USD",
            description="Buy 10 AAPL",
            security_id=sec.id,
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["activityType"] == WF_ACTIVITY_BUY
        assert row["symbol"] == "AAPL"
        assert row["instrumentType"] == "EQUITY"
        assert row["currency"] == "USD"

    def test_map_sale_with_security(self) -> None:
        sec = _make_mock_security(ticker="MSFT")
        txn = _make_mock_transaction(
            transaction_type="sale",
            amount=Decimal("2500.00"),
            currency_code="USD",
            description="Sell 5 MSFT",
            security_id=sec.id,
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["activityType"] == WF_ACTIVITY_SELL
        assert row["symbol"] == "MSFT"
        assert row["instrumentType"] == "EQUITY"

    def test_map_deposit(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="deposit",
            amount=Decimal("1000.00"),
            currency_code="EUR",
            description="Bank transfer",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_DEPOSIT
        assert row["symbol"] == ""
        assert row["quantity"] == "1.00"
        assert row["amount"] == "1000.00"

    def test_map_withdrawal(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="withdrawal",
            amount=Decimal("-500.00"),
            currency_code="EUR",
            description="ATM withdrawal",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_WITHDRAWAL
        assert row["symbol"] == ""
        assert row["amount"] == "500.00"

    def test_map_dividend(self) -> None:
        sec = _make_mock_security(ticker="VOO")
        txn = _make_mock_transaction(
            transaction_type="dividend",
            amount=Decimal("50.00"),
            currency_code="USD",
            description="VOO Dividend",
            security_id=sec.id,
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["activityType"] == WF_ACTIVITY_DIVIDEND
        assert row["symbol"] == "VOO"
        assert row["amount"] == "50.00"

    def test_map_interest(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="interest",
            amount=Decimal("3.42"),
            currency_code="EUR",
            description="Interest payment",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_INTEREST
        assert row["amount"] == "3.42"

    def test_map_fee(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="fee",
            amount=Decimal("-9.99"),
            currency_code="EUR",
            description="Brokerage fee",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_FEE
        assert row["amount"] == "9.99"

    def test_map_transfer_in(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="transfer",
            amount=Decimal("5000.00"),
            currency_code="EUR",
            description="Transfer in",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_TRANSFER_IN

    def test_map_transfer_out(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="transfer",
            amount=Decimal("-2000.00"),
            currency_code="EUR",
            description="Transfer out",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_TRANSFER_OUT

    def test_map_security_by_isin_when_no_ticker(self) -> None:
        sec = _make_mock_security(ticker=None, isin="US0378331005")
        txn = _make_mock_transaction(
            transaction_type="purchase",
            amount=Decimal("-1000.00"),
            security_id=sec.id,
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["symbol"] == "US0378331005"
        assert row["instrumentType"] == "EQUITY"

    def test_map_instrument_type_custom_override(self) -> None:
        sec = _make_mock_security(security_type="etf", ticker="VWCE")
        txn = _make_mock_transaction(
            transaction_type="purchase",
            amount=Decimal("-2000.00"),
            security_id=sec.id,
        )
        custom_map = {"etf": "ETF"}
        row = map_transaction_to_wf_row(
            txn, security=sec, instrument_type_map=custom_map
        )
        assert row["instrumentType"] == "ETF"

    def test_comment_includes_external_id(self) -> None:
        txn = _make_mock_transaction(
            description="Buy AAPL",
            external_transaction_id="txn_ext_001",
        )
        row = map_transaction_to_wf_row(txn)
        assert "Buy AAPL" in row["comment"]
        assert "ID: txn_ext_001" in row["comment"]

    def test_map_holding_with_security(self) -> None:
        sec = _make_mock_security()
        holding = _make_mock_holding(security_id=sec.id)
        row = map_holding_to_wf_row(holding, security=sec)
        assert row["symbol"] == "AAPL"
        assert row["date"] == "2025-06-30"
        assert float(row["quantity"]) == 50.0
        # avgCost = cost_basis / quantity = 8574 / 50
        assert float(row["avgCost"]) == pytest.approx(8574.00 / 50.0, rel=0.01)

    def test_map_holding_without_cost_basis(self) -> None:
        sec = _make_mock_security(ticker="BTC")
        holding = _make_mock_holding(
            security_id=sec.id,
            cost_basis=None,
        )
        row = map_holding_to_wf_row(holding, security=sec)
        assert row["symbol"] == "BTC"
        assert row["avgCost"] == ""

    def test_map_holding_cash(self) -> None:
        """Holdings without a security use UNKNOWN symbol."""
        holding = _make_mock_holding(security_id="nonexistent")
        row = map_holding_to_wf_row(holding, security=None)
        assert row["symbol"] == "UNKNOWN"

    def test_map_transactions_to_csv_content(self) -> None:
        """Full CSV content includes header and all rows."""
        sec = _make_mock_security()
        txns = [
            _make_mock_transaction(
                transaction_type="purchase",
                amount=Decimal("-100.00"),
                security_id=sec.id,
            ),
            _make_mock_transaction(
                transaction_type="dividend",
                amount=Decimal("5.00"),
                description="Dividend",
            ),
        ]
        csv = map_transactions_to_csv(txns, security_map={sec.id: sec})
        assert csv.startswith("date,symbol,")
        assert "BUY" in csv
        assert "DIVIDEND" in csv
        assert csv.count("\n") == 3  # header + 2 rows

    def test_map_transactions_to_csv_empty(self) -> None:
        assert map_transactions_to_csv([]) == ""

    def test_map_holdings_to_csv_content(self) -> None:
        holdings = [_make_mock_holding() for _ in range(2)]
        csv = map_holdings_to_csv(holdings)
        assert csv.startswith("date,symbol,")
        assert csv.count("\n") == 3  # header + 2 rows

    def test_map_holdings_to_csv_empty(self) -> None:
        assert map_holdings_to_csv([]) == ""


# ═══════════════════════════════════════════════════════════════════════
# Tests for WealthfolioExportResult
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioExportResult:
    def test_construct_and_repr(self) -> None:
        r = WealthfolioExportResult(
            status="completed",
            accounts_mapped=2,
            transactions_attempted=10,
            transactions_exported=8,
            transactions_failed=1,
            transactions_skipped=1,
            holdings_exported=5,
            csv_files=["/tmp/transactions.csv"],
            duration_s=2.5,
        )
        assert r.status == "completed"
        assert r.accounts_mapped == 2
        assert r.transactions_exported == 8
        assert r.transactions_failed == 1
        assert r.transactions_skipped == 1
        assert r.holdings_exported == 5
        assert len(r.csv_files) == 1
        rep = repr(r)
        assert "completed" in rep
        assert "8/10" in rep

    def test_failed_result(self) -> None:
        r = WealthfolioExportResult(
            status="failed",
            error_message="Permission denied",
        )
        assert r.status == "failed"
        assert r.error_message == "Permission denied"


# ═══════════════════════════════════════════════════════════════════════
# Tests for WealthfolioExporter
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def wf_config() -> WealthfolioConfig:
    return WealthfolioConfig(
        output_dir=Path("/tmp/test_wealthfolio_exports"),
        default_currency="EUR",
        export_holdings=True,
    )


@pytest.fixture
def exporter(wf_config: WealthfolioConfig) -> WealthfolioExporter:
    """Exporter with a fully mocked session factory."""
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
        tenant_id="tenant_001",
    )


class TestWealthfolioExporter:
    @pytest.mark.asyncio
    async def test_run_export_no_accounts(self, exporter) -> None:
        """No accounts returns completed with zero counts."""
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
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=MagicMock(id=str(uuid4())),
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2020, 1, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
        assert result.transactions_attempted == 0
        assert result.transactions_exported == 0
        assert result.holdings_exported == 0

    @pytest.mark.asyncio
    async def test_run_export_with_account_no_txns(self, exporter) -> None:
        """Account without recent transactions completes gracefully."""
        mock_acct = _make_mock_account()
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
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
        """Transactions generate CSV output."""
        mock_acct = _make_mock_account()
        mock_txns = [
            _make_mock_transaction(
                transaction_type="deposit",
                amount=Decimal("1000.00"),
                description="Test Deposit",
            ),
            _make_mock_transaction(
                transaction_type="fee",
                amount=Decimal("-5.00"),
                description="Test Fee",
            ),
        ]
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_mark_exported",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test.csv"),
            ),
            patch.object(
                exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
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
    async def test_run_export_with_holdings(self, exporter) -> None:
        """Holdings generate separate CSV file."""
        mock_acct = _make_mock_account()
        mock_holdings = [
            _make_mock_holding(
                quantity=Decimal(100),
                cost_basis=Decimal("15000.00"),
            )
        ]
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=mock_holdings,
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_write_csv_file",
                return_value=Path("/tmp/holdings_test.csv"),
            ),
            patch.object(
                exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
        assert result.holdings_exported == 1

    @pytest.mark.asyncio
    async def test_run_export_filtered_account_ids(self, exporter) -> None:
        """Export respects account_ids filter."""
        mock_acct_a = _make_mock_account(name="Account A")
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct_a],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
                account_ids=[mock_acct_a.id],
            )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_run_export_max_transactions(self, exporter) -> None:
        """max_transactions limits the export batch."""
        mock_acct = _make_mock_account()
        mock_txns = [
            _make_mock_transaction(
                transaction_type="deposit", amount=Decimal("100.00")
            )
            for _ in range(10)
        ]
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=mock_txns,
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_mark_exported",
                return_value=None,
            ),
            patch.object(
                exporter,
                "_write_csv_file",
                return_value=Path("/tmp/test.csv"),
            ),
            patch.object(
                exporter,
                "_write_manifest",
                return_value=Path("/tmp/manifest.json"),
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
                max_transactions=5,
            )

        assert result.status == "completed"
        # Should only export the first 5
        assert result.transactions_attempted == 5

    @pytest.mark.asyncio
    async def test_run_export_exception_handling(self, exporter) -> None:
        """Unexpected errors result in a failed export."""
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                side_effect=ValueError("DB connection lost"),
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2020, 1, 1, tzinfo=UTC),
            )

        assert result.status == "failed"
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_resolve_wf_account_name(self, exporter) -> None:
        """Account name overrides work correctly."""
        override_name = await exporter._resolve_wf_account_name(
            "acct_001", "Default Name"
        )
        assert override_name == "Default Name"

        # With override
        exporter._wf_config.account_name_overrides = {
            "acct_001": "Overridden Name"
        }
        override_name = await exporter._resolve_wf_account_name(
            "acct_001", "Default Name"
        )
        assert override_name == "Overridden Name"

    def test_write_csv_file(self, exporter, tmp_path) -> None:
        """CSV file is written correctly."""
        content = "date,symbol,activityType\n2025-01-01,AAPL,BUY\n"
        path = exporter._write_csv_file(
            content=content,
            export_dir=tmp_path,
            prefix="transactions_Brokerage",
            suffix=".csv",
        )
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content
        assert "transactions_Brokerage" in path.name
        assert path.suffix == ".csv"

    def test_write_manifest(self, exporter, tmp_path) -> None:
        """Manifest JSON is written correctly."""
        path = exporter._write_manifest(
            ["/tmp/file1.csv", "/tmp/file2.csv"],
            export_dir=tmp_path,
            attempted=10,
            exported=8,
            holdings=5,
        )
        assert path.exists()
        import json

        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["transactions_attempted"] == 10
        assert manifest["transactions_exported"] == 8
        assert manifest["holdings_exported"] == 5
        assert len(manifest["files"]) == 2

    @pytest.mark.asyncio
    async def test_load_securities(self, exporter) -> None:
        """Securities are loaded into a dict keyed by id."""
        mock_sec = _make_mock_security()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Mock execute to return scalars containing the security
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_sec]
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Patch the session factory
        exporter._session_factory = MagicMock(return_value=mock_session)

        sec_map = await exporter._load_securities()
        assert mock_sec.id in sec_map
        assert sec_map[mock_sec.id] is mock_sec

    @pytest.mark.asyncio
    async def test_account_name_override_in_export(self, exporter) -> None:
        """Account name override is used in CSV filenames."""
        mock_acct = _make_mock_account(id="acct_override", name="Old Name")
        exporter._wf_config.account_name_overrides = {
            "acct_override": "Custom WF Name"
        }
        mock_run = MagicMock(id=str(uuid4()))

        with (
            patch.object(
                exporter,
                "_load_accounts",
                return_value=[mock_acct],
            ),
            patch.object(
                exporter,
                "_load_securities",
                return_value={},
            ),
            patch.object(
                exporter,
                "_fetch_pending_transactions",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_fetch_current_holdings",
                return_value=[],
            ),
            patch.object(
                exporter,
                "_complete_run",
                return_value=None,
            ),
            patch(
                "finance_sync.exporter.wealthfolio.exporter.ExportRun",
                return_value=mock_run,
            ),
        ):
            result = await exporter.run_export(
                since=datetime(2025, 1, 1, tzinfo=UTC),
            )

        assert result.status == "completed"
