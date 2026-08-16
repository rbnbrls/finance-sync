"""Contract checks for the checked-in synthetic staging provider dataset."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from finance_sync.connectors.bunq import BunqConnector
from finance_sync.connectors.trading212 import (
    _parse_cash_transaction,
    _parse_order,
)

DATASET = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "staging"
    / "fixtures"
    / "2026-07"
)


def _json(relative_path: str):
    return json.loads((DATASET / relative_path).read_text(encoding="utf-8"))


def _csv(relative_path: str) -> list[list[str]]:
    with (DATASET / relative_path).open(
        encoding="utf-8-sig", newline=""
    ) as file:
        return list(csv.reader(file))


def test_manifest_and_daily_bunq_coverage() -> None:
    manifest = _json("manifest.json")
    payments = _json("bunq/payments-account-9100001.json")["Response"]
    parsed = [
        BunqConnector._parse_payment(item["Payment"], "9100001")
        for item in payments
    ]

    assert manifest["synthetic"] is True
    assert len(parsed) == 31
    assert {item.occurred_at.date().isoformat() for item in parsed} == set(
        manifest["expected"]["bunq"]["daily_date_coverage"]
    )
    assert all(item.occurred_at.tzinfo == UTC for item in parsed)


def test_trading212_has_weekly_stocks_etfs_dividend_and_holdings() -> None:
    orders = _json("trading212/order-history.json")["items"]
    cash = _json("trading212/transaction-history.json")["items"]
    portfolio = _json("trading212/portfolio.json")
    parsed_orders = [_parse_order(item, "9200001") for item in orders]
    parsed_cash = [_parse_cash_transaction(item, "9200001") for item in cash]
    event_dates = {
        item.occurred_at.date().isoformat()
        for item in parsed_orders + parsed_cash
    }

    assert event_dates == {
        "2026-07-03",
        "2026-07-10",
        "2026-07-17",
        "2026-07-24",
        "2026-07-31",
    }
    assert {item["frontend"] for item in orders} == {"STOCKS", "ETF"}
    assert [item.transaction_type for item in parsed_cash] == ["dividend"]
    assert {item["frontend"] for item in portfolio} == {"STOCKS", "ETF"}


def test_degiro_exports_have_weekly_trades_dividend_tax_and_positions() -> None:
    trades = _csv("degiro/transacties.csv")
    statement = _csv("degiro/rekeningoverzicht.csv")
    portfolio = _csv("degiro/portefeuille.csv")

    assert trades[0][0:6] == [
        "Datum",
        "Tijd",
        "Product",
        "ISIN",
        "Referentiebeurs",
        "Beurs",
    ]
    assert len(trades[1:]) == 4
    activity_dates = {row[0] for row in trades[1:]}
    activity_dates.update(
        row[0]
        for row in statement[1:]
        if row[5] in {"Dividend", "Dividendbelasting"}
    )
    assert activity_dates == {
        "03-07-2026",
        "10-07-2026",
        "17-07-2026",
        "24-07-2026",
        "31-07-2026",
    }
    assert {row[2] for row in trades[1:]} == {
        "Vanguard FTSE All-World UCITS ETF",
        "ASML Holding NV",
        "Apple Inc.",
        "iShares Core MSCI World UCITS ETF",
    }
    assert {row[1] for row in portfolio[1:]} == {
        "IE00BK5BQT80",
        "NL0010273215",
        "US0378331005",
        "IE00B4L5Y983",
    }
    descriptions = [row[5] for row in statement[1:]]
    assert descriptions.count("Dividend") == 1
    assert descriptions.count("Dividendbelasting") == 1


def test_all_records_are_inside_declared_month() -> None:
    month_start = datetime(2026, 7, 1, tzinfo=UTC).date()
    month_end = datetime(2026, 7, 31, tzinfo=UTC).date()
    payments = _json("bunq/payments-account-9100001.json")["Response"]
    dates = [
        datetime.strptime(
            item["Payment"]["created"], "%Y-%m-%d %H:%M:%S.%f"
        ).date()
        for item in payments
    ]
    assert min(dates) == month_start
    assert max(dates) == month_end
