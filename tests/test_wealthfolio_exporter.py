"""Tests for the Wealthfolio exporter service and transaction mapper.

Uses mock DB sessions and realistic fixture data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
    WF_ACTIVITY_TAX,
    WF_ACTIVITY_TRANSFER_IN,
    WF_ACTIVITY_TRANSFER_OUT,
    WF_ACTIVITY_WITHDRAWAL,
    UnresolvedSecurityExportError,
    map_holding_to_wf_row,
    map_holdings_to_csv,
    map_transaction_to_wf_row,
    map_transactions_to_csv,
)
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from collections.abc import Generator

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
        "quantity": None,
        "unit_price": None,
        "fee_amount": None,
        "fee_currency_code": None,
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
        settings.wealthfolio_holdings_strategy = "reconcile"
        settings.wealthfolio_reconciliation_absolute_tolerance = Decimal(1)
        settings.wealthfolio_reconciliation_percentage_tolerance = Decimal(
            "0.005"
        )

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
            quantity=Decimal(10),
            unit_price=Decimal("150.50"),
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["activityType"] == WF_ACTIVITY_BUY
        assert row["symbol"] == "US0378331005"
        assert row["quantity"] == "10.00"
        assert row["unitPrice"] == "150.50"
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
            quantity=Decimal(5),
            unit_price=Decimal(500),
        )
        row = map_transaction_to_wf_row(txn, security=sec)
        assert row["activityType"] == WF_ACTIVITY_SELL
        assert row["symbol"] == "US0378331005"
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
        assert row["symbol"] == "US0378331005"
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

    def test_map_tax(self) -> None:
        txn = _make_mock_transaction(
            transaction_type="tax",
            amount=Decimal("-7.50"),
            currency_code="EUR",
            description="Dividend withholding tax",
        )
        row = map_transaction_to_wf_row(txn)
        assert row["activityType"] == WF_ACTIVITY_TAX
        assert row["amount"] == "7.50"

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
            quantity=Decimal(10),
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
            quantity=Decimal(20),
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
        assert row["symbol"] == "US0378331005"
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
        assert row["symbol"] == "US0378331005"
        assert row["avgCost"] == ""

    def test_map_holding_cash(self) -> None:
        """Holdings without a security enter the review flow."""
        holding = _make_mock_holding(security_id="nonexistent")
        with pytest.raises(UnresolvedSecurityExportError):
            map_holding_to_wf_row(holding, security=None)

    def test_map_transactions_to_csv_content(self) -> None:
        """Full CSV content includes header and all rows."""
        sec = _make_mock_security()
        txns = [
            _make_mock_transaction(
                transaction_type="purchase",
                amount=Decimal("-100.00"),
                security_id=sec.id,
                quantity=Decimal(1),
                unit_price=Decimal(100),
            ),
            _make_mock_transaction(
                transaction_type="dividend",
                amount=Decimal("5.00"),
                description="Dividend",
                security_id=sec.id,
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
        securities = {
            holding.security_id: _make_mock_security(id=holding.security_id)
            for holding in holdings
        }
        csv = map_holdings_to_csv(holdings, security_map=securities)
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
        assert result.run_id == str(mock_run.id)

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
        mock_security = _make_mock_security(id=mock_holdings[0].security_id)
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
                return_value={mock_security.id: mock_security},
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


# ═══════════════════════════════════════════════════════════════════════
# Tests for push_to_wealthfolio — delivery cursor + DLQ (Gap G-14)
# ═══════════════════════════════════════════════════════════════════════


class TestWealthfolioPushCursor:
    """Idempotent push resume via the per-account delivery cursor."""

    def _mock_wf_client(self, **push_overrides: Any) -> MagicMock:
        """Client whose push_activities returns configurable counts."""
        client = MagicMock()
        result = {"imported": 0, "skipped": 0, "failed": 0}
        result.update(push_overrides)
        client.push_activities = AsyncMock(return_value=result)
        return client

    def _patch_push_deps(
        self,
        exporter: WealthfolioExporter,
        *,
        accounts: list[MagicMock],
        txns_by_account: dict[str, list[MagicMock]],
    ) -> tuple[MagicMock, AsyncMock, AsyncMock]:
        """Patch push internals; return (wf_client, fetch_mock, complete_mock)."""
        wf_client = self._mock_wf_client()

        async def _fake_fetch(
            *,
            account_id: str,
            since: datetime,
            after: tuple[datetime, UUID] | None = None,
        ) -> list:
            return txns_by_account.get(account_id, [])

        fetch_mock = AsyncMock(side_effect=_fake_fetch)
        complete_mock = AsyncMock(return_value=None)
        update_mock = AsyncMock(return_value=None)

        patch.object(
            exporter,
            "_last_export_time",
            return_value=datetime(2020, 1, 1, tzinfo=UTC),
        ).start()
        patch.object(exporter, "_load_accounts", return_value=accounts).start()
        patch.object(exporter, "_load_securities", return_value={}).start()
        patch.object(
            exporter,
            "_ensure_wf_account",
            return_value={"id": "wf-account-1", "name": "Brokerage"},
        ).start()
        patch.object(
            exporter,
            "_sync_and_reconcile_holdings",
            return_value=[],
        ).start()
        patch.object(
            exporter, "_fetch_pending_transactions", fetch_mock
        ).start()
        patch.object(
            exporter, "_update_wealthfolio_delivery", update_mock
        ).start()
        patch.object(exporter, "_complete_run", complete_mock).start()
        patch(
            "finance_sync.exporter.wealthfolio.exporter.ExportRun",
            return_value=MagicMock(id=str(uuid4())),
        ).start()
        return wf_client, fetch_mock, complete_mock

    @pytest.mark.asyncio
    async def test_push_resumes_from_delivery_cursor(
        self, exporter: WealthfolioExporter
    ) -> None:
        """Cursor ``(occurred_at, id)`` wins over the fallback ``since``."""
        acct = _make_mock_account()
        txn = _make_mock_transaction()
        cursor_ts = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        cursor_id = str(uuid4())

        delivery = MagicMock(
            last_exported_at=cursor_ts,
            last_exported_transaction_id=cursor_id,
        )
        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=delivery
        ):
            wf_client, fetch_mock, _complete_mock = self._patch_push_deps(
                exporter,
                accounts=[acct],
                txns_by_account={acct.id: [txn]},
            )
            wf_client.push_activities.return_value = {
                "imported": 1,
                "skipped": 0,
                "failed": 0,
            }
            fallback = datetime(2020, 1, 1, tzinfo=UTC)
            result = await exporter.push_to_wealthfolio(
                wf_client,
                since=fallback,
            )

        # Fetch resumed strictly after the (occurred_at, id) cursor, not
        # from the fallback timestamp (and not from the cursor timestamp
        # either — the boundary transaction must not be re-fetched).
        kwargs = fetch_mock.await_args.kwargs
        assert kwargs["after"] == (cursor_ts, UUID(cursor_id))
        assert kwargs["since"] == fallback
        assert result["imported"] == 1
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_push_uses_fallback_when_no_cursor(
        self, exporter: WealthfolioExporter
    ) -> None:
        """No cursor yet → falls back to the provided/derived since."""
        acct = _make_mock_account()
        txn = _make_mock_transaction()

        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, fetch_mock, _complete_mock = self._patch_push_deps(
                exporter,
                accounts=[acct],
                txns_by_account={acct.id: [txn]},
            )
            wf_client.push_activities.return_value = {
                "imported": 1,
                "skipped": 0,
                "failed": 0,
            }
            fallback = datetime(2021, 3, 1, tzinfo=UTC)
            await exporter.push_to_wealthfolio(wf_client, since=fallback)

        assert fetch_mock.await_args.kwargs["since"] == fallback

    @pytest.mark.asyncio
    async def test_push_advances_cursor_after_success(
        self, exporter: WealthfolioExporter
    ) -> None:
        """Cursor is updated per account after a successful push."""
        acct = _make_mock_account()
        txns = [_make_mock_transaction() for _ in range(2)]

        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, _fetch_mock, complete_mock = self._patch_push_deps(
                exporter,
                accounts=[acct],
                txns_by_account={acct.id: txns},
            )
            wf_client.push_activities.return_value = {
                "imported": 2,
                "skipped": 0,
                "failed": 0,
            }
            result = await exporter.push_to_wealthfolio(wf_client)

        update_mock = exporter._update_wealthfolio_delivery
        assert update_mock.await_count == 1
        kwargs = update_mock.await_args.kwargs
        assert kwargs["account_id"] == acct.id
        assert kwargs["transactions"] == txns
        assert kwargs["export_run_id"] is not None

        # Run completed with the imported counts
        complete_kwargs = complete_mock.await_args.kwargs
        assert complete_kwargs["status"] == "completed"
        assert complete_kwargs["attempted"] == 2
        assert complete_kwargs["exported"] == 2
        assert complete_kwargs["failed"] == 0
        assert result["run_id"] is not None
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_push_partial_failure_keeps_cursor_and_records_error(
        self, exporter: WealthfolioExporter
    ) -> None:
        """A failing account does not abort the push and keeps its cursor.

        The successful account's cursor advances; the failed account's
        cursor stays put so a retry re-processes only that account.
        """
        acct_ok = _make_mock_account(name="Good Broker")
        acct_bad = _make_mock_account(name="Bad Broker")
        txns_ok = [_make_mock_transaction(account_id=acct_ok.id)]
        txns_bad = [
            _make_mock_transaction(account_id=acct_bad.id) for _ in range(2)
        ]

        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, _fetch_mock, complete_mock = self._patch_push_deps(
                exporter,
                accounts=[acct_ok, acct_bad],
                txns_by_account={
                    acct_ok.id: txns_ok,
                    acct_bad.id: txns_bad,
                },
            )
            wf_client.push_activities = AsyncMock(
                side_effect=[
                    {"imported": 1, "skipped": 0, "failed": 0},
                    RuntimeError("Wealthfolio API rejected the batch"),
                ]
            )
            result = await exporter.push_to_wealthfolio(wf_client)

        # Failure recorded, run marked failed, counts accurate
        assert result["failed"] == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["account_name"] == "Bad Broker"
        assert "rejected" in result["errors"][0]["error"]

        complete_kwargs = complete_mock.await_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert "1 account(s) failed to push" in complete_kwargs["error_message"]

        # Cursor only advanced for the successful account
        update_mock = exporter._update_wealthfolio_delivery
        assert update_mock.await_count == 1
        assert update_mock.await_args.kwargs["account_id"] == acct_ok.id

    @pytest.mark.asyncio
    async def test_push_api_rejected_batch_keeps_cursor(
        self, exporter: WealthfolioExporter
    ) -> None:
        """API-reported failures in a batch do not advance the cursor."""
        acct = _make_mock_account(name="Partial Broker")
        txns = [_make_mock_transaction(account_id=acct.id) for _ in range(3)]

        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, _fetch_mock, complete_mock = self._patch_push_deps(
                exporter,
                accounts=[acct],
                txns_by_account={acct.id: txns},
            )
            # API accepts 1, skips 1, rejects 1 — cursor must not advance
            wf_client.push_activities.return_value = {
                "imported": 1,
                "skipped": 1,
                "failed": 1,
            }
            result = await exporter.push_to_wealthfolio(wf_client)

        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert "rejected 1 of 3" in result["errors"][0]["error"]

        # Cursor NOT advanced → retry re-pushes this account's batch
        update_mock = exporter._update_wealthfolio_delivery
        assert update_mock.await_count == 0

        complete_kwargs = complete_mock.await_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert "1 account(s) failed to push" in complete_kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_push_no_accounts_completes_cleanly(
        self, exporter: WealthfolioExporter
    ) -> None:
        """No accounts → completed run with zero counts and no errors."""
        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, _fetch_mock, complete_mock = self._patch_push_deps(
                exporter, accounts=[], txns_by_account={}
            )
            result = await exporter.push_to_wealthfolio(wf_client)

        assert result == {
            "imported": 0,
            "skipped": 0,
            "failed": 0,
            "run_id": result["run_id"],
            "errors": [],
        }
        complete_kwargs = complete_mock.await_args.kwargs
        assert complete_kwargs["status"] == "completed"
        assert complete_kwargs["attempted"] == 0

    @pytest.mark.asyncio
    async def test_push_fatal_error_marks_run_failed_and_reraises(
        self, exporter: WealthfolioExporter
    ) -> None:
        """Unexpected errors outside the per-account loop → failed run."""
        with patch.object(
            exporter, "_get_wealthfolio_delivery", return_value=None
        ):
            wf_client, _fetch_mock, complete_mock = self._patch_push_deps(
                exporter, accounts=[_make_mock_account()], txns_by_account={}
            )
            # _load_securities is patched by _patch_push_deps; break it
            # after patching by re-patching with a raising side effect.
            exporter._load_securities = AsyncMock(
                side_effect=RuntimeError("DB connection lost")
            )
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await exporter.push_to_wealthfolio(wf_client)

        complete_kwargs = complete_mock.await_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert complete_kwargs["error_message"] is not None


# ═══════════════════════════════════════════════════════════════════════
# Delivery-cursor resume boundary — real query semantics (G-14 follow-up)
# ═══════════════════════════════════════════════════════════════════════

# Review finding: the initial timestamp-only cursor resumed with
# ``occurred_at >= cursor``, re-fetching (and re-pushing) the boundary
# transaction on every run — and a timestamp-only cursor cannot
# represent multiple transactions at one instant.  The fixed cursor is
# a strict ``(occurred_at, id)`` tuple; these tests pin that behaviour
# against the real query on a real (SQLite) session.


class TestFetchPendingTransactionsCursor:
    """Real-query tests for the delivery-cursor resume boundary."""

    # tenant_id / account_id inherit the UUID type from their FK targets,
    # so the real-query tests must use uuid objects (the SQLite bind
    # processor for non-native UUID columns requires them).
    _TENANT_ID = uuid4()
    _ACCOUNT_ID = uuid4()

    @pytest.fixture
    def session_factory(
        self,
    ) -> Generator[async_sessionmaker[AsyncSession], None, None]:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=engine, expire_on_commit=False
        )

        async def _setup() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Transaction.__table__.create)

        asyncio.run(_setup())
        yield factory
        asyncio.run(engine.dispose())

    @pytest.fixture
    def db_exporter(
        self,
        wf_config: WealthfolioConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> WealthfolioExporter:
        return WealthfolioExporter(
            session_factory=session_factory,
            wf_config=wf_config,
            tenant_id=self._TENANT_ID,  # type: ignore[arg-type]
        )

    async def _seed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        txns: list[Transaction],
    ) -> None:
        async with session_factory() as session:
            session.add_all(txns)
            await session.commit()

    def _txn(
        self,
        *,
        id: Any,
        occurred_at: datetime,
        account_id: UUID | None = None,
    ) -> Transaction:
        return Transaction(
            id=id,
            tenant_id=self._TENANT_ID,
            provider_key="trading212",
            external_transaction_id=f"ext_{id}",
            account_id=account_id or self._ACCOUNT_ID,
            amount=Decimal("-42.50"),
            currency_code="EUR",
            occurred_at=occurred_at,
            transaction_type="payment",
            status="booked",
            revision=1,
        )

    @pytest.mark.asyncio
    async def test_fetch_without_cursor_uses_since_inclusive(
        self,
        db_exporter: WealthfolioExporter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No cursor yet → the fallback ``since`` bound is inclusive."""
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        boundary = uuid4()
        later = uuid4()
        await self._seed(
            session_factory,
            [
                self._txn(id=boundary, occurred_at=ts),
                self._txn(id=later, occurred_at=ts + timedelta(minutes=5)),
            ],
        )

        fetched = await db_exporter._fetch_pending_transactions(
            account_id=self._ACCOUNT_ID,
            since=ts,
        )

        assert {t.id for t in fetched} == {boundary, later}

    @pytest.mark.asyncio
    async def test_fetch_with_cursor_excludes_boundary_transaction(
        self,
        db_exporter: WealthfolioExporter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Resume after a successful push must NOT re-fetch the boundary.

        Regression for the off-by-one where ``occurred_at >= cursor``
        re-pushed the last delivered transaction on every run.
        """
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        boundary = uuid4()
        later = uuid4()
        much_later = uuid4()
        await self._seed(
            session_factory,
            [
                self._txn(id=boundary, occurred_at=ts),
                self._txn(id=later, occurred_at=ts + timedelta(minutes=5)),
                self._txn(id=much_later, occurred_at=ts + timedelta(days=1)),
            ],
        )

        # With a cursor at the boundary transaction, resume strictly
        # after it: boundary excluded, everything later included.
        fetched = await db_exporter._fetch_pending_transactions(
            account_id=self._ACCOUNT_ID,
            since=ts - timedelta(days=90),
            after=(ts, boundary),
        )

        assert {t.id for t in fetched} == {later, much_later}

    @pytest.mark.asyncio
    async def test_fetch_with_cursor_includes_same_instant_siblings(
        self,
        db_exporter: WealthfolioExporter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Same-instant transactions after the cursor id are included.

        A timestamp-only cursor would skip them (or re-push the
        boundary); the ``(occurred_at, id)`` tuple disambiguates.
        """
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        # Deterministic ordering: first < second in stored (hex) order.
        first, second = uuid4(), uuid4()
        while f"{first.hex}" > f"{second.hex}":
            second = uuid4()
        later = uuid4()
        await self._seed(
            session_factory,
            [
                self._txn(id=first, occurred_at=ts),
                self._txn(id=second, occurred_at=ts),
                self._txn(id=later, occurred_at=ts + timedelta(minutes=5)),
            ],
        )

        fetched = await db_exporter._fetch_pending_transactions(
            account_id=self._ACCOUNT_ID,
            since=ts - timedelta(days=90),
            after=(ts, first),
        )

        assert {t.id for t in fetched} == {second, later}

    @pytest.mark.asyncio
    async def test_fetch_with_cursor_ignores_since_for_later_instants(
        self,
        db_exporter: WealthfolioExporter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cursor wins over ``since`` even when ``since`` is earlier."""
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        boundary = uuid4()
        earlier_than_cursor = uuid4()
        later = uuid4()
        await self._seed(
            session_factory,
            [
                self._txn(
                    id=earlier_than_cursor,
                    occurred_at=ts - timedelta(hours=1),
                ),
                self._txn(id=boundary, occurred_at=ts),
                self._txn(id=later, occurred_at=ts + timedelta(minutes=5)),
            ],
        )

        fetched = await db_exporter._fetch_pending_transactions(
            account_id=self._ACCOUNT_ID,
            since=ts - timedelta(days=90),
            after=(ts, boundary),
        )

        # The transaction before the cursor (even though after ``since``)
        # must not be re-fetched — it was already delivered.
        assert {t.id for t in fetched} == {later}
