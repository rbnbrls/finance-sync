"""Contract and golden-fixture tests for the DEGIRO pension importer."""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from finance_sync.connectors.degiro_pension import DegiroPensionConnector
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig

FIXTURES = Path(__file__).parent / "fixtures"


def _connector(*names: str, **options: object) -> DegiroPensionConnector:
    return DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={
                "export_paths": [str(FIXTURES / name) for name in names],
                "account_key": "synthetic-pension-fixture",
                "snapshot_at": "2026-02-20",
                **options,
            },
        )
    )


@pytest.mark.asyncio
async def test_contract_and_all_report_types() -> None:
    connector = _connector(
        "transactions_nl.csv",
        "account_statement_en.csv",
        "portfolio_nl.csv",
    )
    await connector.authenticate()

    assert connector.name == "degiro_pension"
    assert connector.display_name == "DEGIRO Pensioen"
    assert connector.supported_resources == {
        "accounts",
        "transactions",
        "holdings",
    }
    assert connector.validation_report.successful
    assert connector.validation_report.report_types == {
        "transactions",
        "account_statement",
        "portfolio",
    }

    accounts = await connector.fetch_accounts()
    assert len(accounts) == 1
    account = accounts[0]
    assert account.account_type == "investment"
    assert account.account_subtype == "nl_lijfrente"
    assert account.current_balance == Decimal("650.00")
    assert account.available_balance == Decimal("20.95")
    assert "synthetic-pension-fixture" not in account.external_account_id

    transactions = await connector.fetch_transactions(
        datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert len(transactions) == 8
    assert {item.transaction_type for item in transactions} >= {
        "purchase",
        "sale",
        "deposit",
        "dividend",
        "tax",
        "fee",
        "interest",
    }
    assert (
        sum(item.transaction_type == "purchase" for item in transactions) == 2
    )
    assert len({item.external_transaction_id for item in transactions}) == 8
    assert all(isinstance(item.amount, Decimal) for item in transactions)
    assert connector.validation_report.rows_skipped == 5

    holdings = await connector.fetch_holdings()
    assert len(holdings) == 2
    assert all(item.security_reference.isin for item in holdings)
    assert holdings[0].cost_basis == Decimal("200.050")


@pytest.mark.asyncio
async def test_reimport_and_overlapping_exports_are_idempotent() -> None:
    connector = _connector("transactions_nl.csv", "transactions_nl.csv")
    await connector.authenticate()
    first = await connector.fetch_transactions(datetime(2020, 1, 1, tzinfo=UTC))
    await connector.authenticate()
    second = await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert len(first) == 3
    assert [item.external_transaction_id for item in first] == [
        item.external_transaction_id for item in second
    ]


@pytest.mark.asyncio
async def test_since_account_and_limit_filters() -> None:
    connector = _connector("transactions_nl.csv")
    await connector.authenticate()
    assert await connector.fetch_transactions(
        datetime(2026, 2, 1, tzinfo=UTC), limit=1
    )
    assert not await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC), account_id="other"
    )
    assert not await connector.fetch_holdings(account_id="other")


@pytest.mark.asyncio
async def test_current_12_column_statement_with_blank_currency_headers() -> (
    None
):
    connector = _connector("account_statement_nl_12.csv")
    await connector.authenticate()
    transactions = await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert len(transactions) == 1
    assert transactions[0].transaction_type == "dividend"
    assert transactions[0].currency_code == "USD"


@pytest.mark.asyncio
async def test_statement_pairs_usd_dividend_with_degiro_fx_conversion(
    tmp_path: Path,
) -> None:
    """Use DEGIRO's technical FX debit as the dividend's base value."""
    path = tmp_path / "account_statement.csv"
    path.write_text(
        "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id\n"
        "2026-08-12,09:00,2026-08-11,,,Valuta Creditering,,EUR,106.64,EUR,100.00,\n"
        "2026-08-12,09:00,2026-08-11,,,Valuta Debitering,1.166,USD,-124.34,USD,0.0,\n"
        "2026-08-11,07:00,2026-08-07,Fund A,IE000U5MJOZ6,Dividend,,USD,83.0,USD,83.0,\n"
        "2026-08-10,07:00,2026-08-07,Fund B,IE000U5MJOZ7,Dividend,,USD,41.34,USD,41.34,\n",
        encoding="utf-8",
    )
    connector = _connector(str(path))
    await connector.authenticate()
    transactions = await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC)
    )
    dividends = [
        transaction
        for transaction in transactions
        if transaction.transaction_type == "dividend"
    ]
    assert len(dividends) == 2
    assert all(transaction.currency_code == "USD" for transaction in dividends)
    assert all(
        transaction.amount_in_base is not None for transaction in dividends
    )
    assert all(
        transaction.amount_in_base == expected
        for transaction, expected in zip(
            dividends,
            (
                Decimal("35.45454545454545454545454545"),
                Decimal("71.18353344768439108061749571"),
            ),
            strict=True,
        )
    )
    assert all(
        transaction.base_currency_code == "EUR" for transaction in dividends
    )
    assert all(
        transaction.fx_rate == Decimal("1.166") for transaction in dividends
    )


@pytest.mark.asyncio
async def test_statement_reads_dutch_exchange_rate_for_usd_cash_activity(
    tmp_path: Path,
) -> None:
    """The Dutch statement export labels the FX column ``Wisselkoers``."""
    path = tmp_path / "account_statement.csv"
    path.write_text(
        "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,Wisselkoers,Mutatie,,Saldo,,Order ID\n"
        "2026-07-17,08:00,2026-07-17,Apple Inc.,US0378331005,Dividend,1.1500,0.25,USD,0.25,USD,\n",
        encoding="utf-8",
    )
    connector = _connector(str(path))
    await connector.authenticate()
    transactions = await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert len(transactions) == 1
    assert transactions[0].fx_rate == Decimal("1.1500")
    assert transactions[0].amount_in_base == Decimal("0.25") / Decimal("1.15")


@pytest.mark.asyncio
async def test_empty_portfolio_is_a_zero_value_snapshot() -> None:
    connector = _connector("portfolio_empty_en.csv")
    await connector.authenticate()
    account = (await connector.fetch_accounts())[0]
    assert account.current_balance == Decimal(0)
    assert account.available_balance == Decimal(0)
    assert await connector.fetch_holdings() == []


@pytest.mark.asyncio
async def test_current_portfolio_csv_upload_layout_with_unlabelled_currency(
    tmp_path: Path,
) -> None:
    """Accept the Portfolio.csv layout downloaded from the DEGIRO UI."""
    portfolio = tmp_path / "Portfolio.csv"
    portfolio.write_text(
        "Product,Symbool/ISIN,Aantal,Slotkoers,Lokale waarde,,Waarde in EUR\n"
        'CASH & CASH FUND & FTX CASH (EUR),,,,EUR,"9587,44","9587,44"\n'
        'ALFEN NV,NL0012817175,100,"14,25",EUR,"1425,00","1425,00"\n'
        'SERVICENOW INC,US81762P1021,20,"117,70",USD,"2354,00","2032,68"\n',
        encoding="utf-8",
    )
    connector = DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={
                "export_path": str(portfolio),
                "account_key": "portfolio-upload-layout",
                "snapshot_at": "2026-08-18",
            },
        )
    )

    await connector.authenticate()

    account = (await connector.fetch_accounts())[0]
    holdings = await connector.fetch_holdings()
    assert connector.validation_report.report_types == {"portfolio"}
    assert len(holdings) == 2
    assert account.available_balance == Decimal("9587.44")
    assert account.current_balance == Decimal("13045.12")
    assert holdings[0].market_value == Decimal("1425.00")
    assert holdings[0].currency_code == "EUR"
    assert holdings[1].market_value == Decimal("2032.68")
    assert holdings[1].price_currency == "USD"


@pytest.mark.asyncio
async def test_xlsx_export_is_supported(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(
        [
            "Product",
            "Symbol/ISIN",
            "Quantity",
            "Closing price",
            "Currency",
            "Local value",
            "Value in EUR",
            "Average price",
        ]
    )
    sheet.append(["Test Fund", "IE00B4L5Y983", 2, 25, "EUR", 50, 50, 20])
    path = tmp_path / "anonymous.xlsx"
    workbook.save(path)
    connector = DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={"export_path": str(path), "account_key": "xlsx-test"},
        )
    )
    await connector.authenticate()
    assert (await connector.fetch_accounts())[0].current_balance == Decimal(50)
    assert len(await connector.fetch_holdings()) == 1


@pytest.mark.asyncio
async def test_current_transactions_export_keeps_trade_and_autofx_costs(
    tmp_path: Path,
) -> None:
    """The current DEGIRO header includes a EUR suffix on the fee column."""
    path = tmp_path / "Transactions.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(
        [
            "Datum",
            "Tijd",
            "Product",
            "ISIN",
            "Beurs",
            "Uitvoeringsplaats",
            "Aantal",
            "Koers",
            "",
            "Lokale waarde",
            "",
            "Waarde EUR",
            "Wisselkoers",
            "AutoFX Kosten",
            "Transactiekosten en/of kosten van derden EUR",
            "Totaal EUR",
            "Order ID",
        ]
    )
    sheet.append(
        [
            "25-08-2026",
            "16:30",
            "Test ETF",
            "IE00B4L5Y983",
            "TDG",
            "XGAT",
            100,
            21.42,
            "EUR",
            -2142,
            "EUR",
            -2142,
            "",
            -0.25,
            -1.0,
            -2143.25,
            "ORDER-1",
        ]
    )
    workbook.save(path)

    connector = DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={"export_path": str(path), "account_key": "fee-xlsx"},
        )
    )
    await connector.authenticate()
    transactions = await connector.fetch_transactions(
        datetime(2020, 1, 1, tzinfo=UTC)
    )

    assert len(transactions) == 1
    assert transactions[0].fee_amount == Decimal("1.25")
    assert transactions[0].fee_currency_code == "EUR"
    assert transactions[0].provider_metadata is not None
    assert transactions[0].provider_metadata["transaction_fee"] == "-1"
    assert transactions[0].provider_metadata["autofx_fee"] == "-0.25"


@pytest.mark.asyncio
async def test_pdf_has_a_non_technical_error(tmp_path: Path) -> None:
    path = tmp_path / "export.pdf"
    path.write_bytes(b"%PDF synthetic")
    connector = DegiroPensionConnector(
        ConnectorConfig(
            provider_type="degiro_pension",
            options={"export_path": str(path)},
        )
    )
    with pytest.raises(PermanentError, match=r"PDF-bestanden.*CSV of Excel"):
        await connector.authenticate()


@pytest.mark.asyncio
async def test_malformed_input_exposes_validation_report() -> None:
    connector = _connector("malformed.csv")
    with pytest.raises(PermanentError, match="ongeldige regel"):
        await connector.authenticate()
    assert not connector.validation_report.successful
    assert connector.validation_report.errors
    assert "malformed.csv" in connector.validation_report.errors[0]
