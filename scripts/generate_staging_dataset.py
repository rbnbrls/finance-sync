#!/usr/bin/env python3
"""Generate the deterministic July 2026 staging provider dataset.

The output is intentionally synthetic and contains no real account data. Run
this script after changing a scenario so the checked-in static files stay
reproducible.
"""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deploy" / "staging" / "fixtures" / "2026-07"


def _write_json(relative_path: str, value: object) -> None:
    path = OUTPUT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(
    relative_path: str, headers: list[str], rows: list[list[str]]
) -> None:
    path = OUTPUT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _bunq_payment(
    day: int,
    amount: str,
    description: str,
    payment_type: str,
    counterparty: str,
) -> dict[str, object]:
    timestamp = f"2026-07-{day:02d} 12:00:00.000000"
    return {
        "Payment": {
            "id": 7202607000 + day,
            "created": timestamp,
            "updated": f"2026-07-{day:02d} 12:02:00.000000",
            "monetary_account_id": 9100001,
            "amount": {"value": amount, "currency": "EUR"},
            "description": description,
            "type": payment_type,
            "status": "ACCEPTED",
            "sub_type": None,
            "counterparty_alias": {
                "type": "IBAN",
                "value": f"NL00STAGE{day:010d}",
                "name": counterparty,
            },
            "attachment": [],
        }
    }


def _generate_bunq() -> int:
    scenarios = [
        ("3250.00", "Salaris juli", "DEPOSIT", "Staging Werkgever"),
        ("-1250.00", "Huur juli", "SDD", "Staging Verhuurder"),
        ("-54.32", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-4.20", "Koffie", "PAYMENT", "Staging Koffiebar"),
        ("-32.50", "Openbaar vervoer", "BILLING", "Staging Mobiliteit"),
        ("-68.40", "Energie", "SDD", "Staging Energie"),
        ("-18.75", "Lunch", "PAYMENT", "Staging Lunchroom"),
        ("-24.95", "Apotheek", "PAYMENT", "Staging Apotheek"),
        ("-12.00", "Streaming", "SDD", "Staging Streaming"),
        ("-75.00", "Pensioeninleg", "TRANSFER", "DEGIRO Pensioen"),
        ("-43.21", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-29.99", "Kleding", "PAYMENT", "Staging Warenhuis"),
        ("-5.10", "Koffie", "PAYMENT", "Staging Koffiebar"),
        ("-36.00", "Brandstof", "PAYMENT", "Staging Tankstation"),
        ("-14.50", "Lunch", "PAYMENT", "Staging Lunchroom"),
        ("-22.40", "Internet", "SDD", "Staging Telecom"),
        ("-61.25", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-8.75", "Bakker", "PAYMENT", "Staging Bakker"),
        ("-19.99", "Software abonnement", "BILLING", "Staging Software"),
        ("-125.00", "Verzekering", "SDD", "Staging Verzekeraar"),
        ("-47.60", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-3.85", "Koffie", "PAYMENT", "Staging Koffiebar"),
        ("-26.50", "Restaurant", "PAYMENT", "Staging Restaurant"),
        ("-75.00", "Pensioeninleg", "TRANSFER", "DEGIRO Pensioen"),
        ("-39.80", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-16.25", "Bioscoop", "PAYMENT", "Staging Bioscoop"),
        ("-4.60", "Koffie", "PAYMENT", "Staging Koffiebar"),
        ("-30.00", "Openbaar vervoer", "BILLING", "Staging Mobiliteit"),
        ("-52.10", "Boodschappen", "PAYMENT", "Staging Supermarkt"),
        ("-95.00", "Eigen risico", "SDD", "Staging Zorgverzekeraar"),
        ("-8.50", "Lunch", "PAYMENT", "Staging Lunchroom"),
    ]
    payments = [
        _bunq_payment(day, *scenario)
        for day, scenario in enumerate(scenarios, start=1)
    ]
    payments.reverse()  # bunq returns newest first
    net_change = sum(Decimal(item[0]) for item in scenarios)
    closing_balance = Decimal("2500.00") + net_change

    _write_json(
        "bunq/session-server.json",
        {
            "Response": [
                {"Token": {"id": 990001, "token": "staging_session_token"}},
                {
                    "UserPerson": {
                        "id": 9900001,
                        "public_uuid": "staging-user-0001",
                        "display_name": "Staging User",
                    }
                },
            ],
            "Pagination": {"future_url": None},
        },
    )
    _write_json(
        "bunq/monetary-accounts.json",
        {
            "Response": [
                {
                    "MonetaryAccountBank": {
                        "id": 9100001,
                        "created": "2026-01-01 09:00:00.000000",
                        "updated": "2026-07-31 23:59:00.000000",
                        "description": "Staging Betaalrekening",
                        "status": "ACTIVE",
                        "sub_type": "CURRENT",
                        "balance": {
                            "value": f"{closing_balance:.2f}",
                            "currency": "EUR",
                        },
                        "alias": [
                            {
                                "type": "IBAN",
                                "value": "NL00STAGE0000000001",
                                "name": "Staging User",
                            }
                        ],
                    }
                }
            ],
            "Pagination": {"future_url": None},
        },
    )
    _write_json(
        "bunq/payments-account-9100001.json",
        {"Response": payments, "Pagination": {"future_url": None}},
    )
    return len(payments)


def _t212_order(
    identifier: int,
    timestamp: str,
    ticker: str,
    quantity: float,
    price: float,
    frontend: str,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "ticker": ticker,
        "type": "MARKET",
        "side": "BUY",
        "quantity": quantity,
        "filledQuantity": quantity,
        "price": None,
        "filledPrice": price,
        "total": round(quantity * price, 2),
        "status": "FILLED",
        "creationTime": timestamp,
        "filledTime": timestamp.replace("00.000Z", "30.000Z"),
        "currencyCode": "EUR",
        "tax": 0.0,
        "stampDuty": 0.0,
        "executionVenue": "SMART",
        "frontend": frontend,
    }


def _generate_trading212() -> tuple[int, int]:
    orders = [
        _t212_order(
            820260703, "2026-07-03T10:00:00.000Z", "VWCE.DE", 2, 130, "ETF"
        ),
        _t212_order(
            820260710, "2026-07-10T10:00:00.000Z", "AAPL", 1, 195, "STOCKS"
        ),
        _t212_order(
            820260724, "2026-07-24T10:00:00.000Z", "ASML.NL", 0.5, 680, "STOCKS"
        ),
        _t212_order(
            820260731, "2026-07-31T10:00:00.000Z", "IWDA.NL", 3, 91, "ETF"
        ),
    ]
    cash_transactions = [
        {
            "id": 830260717,
            "type": "DIVIDEND",
            "dateTime": "2026-07-17T08:00:00.000Z",
            "amount": 0.25,
            "currencyCode": "EUR",
            "reference": "Synthetic AAPL dividend",
            "ticker": "AAPL",
        }
    ]
    portfolio = [
        {
            "ticker": "VWCE.DE",
            "quantity": 2.0,
            "averagePrice": 130.0,
            "currentPrice": 132.0,
            "initialFillDate": "2026-07-03T10:00:30.000Z",
            "frontend": "ETF",
            "currencyCode": "EUR",
        },
        {
            "ticker": "AAPL",
            "quantity": 1.0,
            "averagePrice": 195.0,
            "currentPrice": 198.0,
            "initialFillDate": "2026-07-10T10:00:30.000Z",
            "frontend": "STOCKS",
            "currencyCode": "EUR",
        },
        {
            "ticker": "ASML.NL",
            "quantity": 0.5,
            "averagePrice": 680.0,
            "currentPrice": 700.0,
            "initialFillDate": "2026-07-24T10:00:30.000Z",
            "frontend": "STOCKS",
            "currencyCode": "EUR",
        },
        {
            "ticker": "IWDA.NL",
            "quantity": 3.0,
            "averagePrice": 91.0,
            "currentPrice": 92.0,
            "initialFillDate": "2026-07-31T10:00:30.000Z",
            "frontend": "ETF",
            "currencyCode": "EUR",
        },
    ]
    invested = sum(
        item["quantity"] * item["currentPrice"] for item in portfolio
    )
    _write_json(
        "trading212/account-info.json",
        {"id": 9200001, "currencyCode": "EUR"},
    )
    _write_json(
        "trading212/account-cash.json",
        {
            "free": 1250.0,
            "invested": round(invested, 2),
            "result": 25.0,
            "blocked": 0.0,
            "pending": 0.0,
            "pieCash": 0.0,
            "currencyCode": "EUR",
        },
    )
    _write_json(
        "trading212/order-history.json",
        {"items": list(reversed(orders)), "nextPagePath": None},
    )
    _write_json(
        "trading212/transaction-history.json",
        {"items": cash_transactions, "nextPagePath": None},
    )
    _write_json("trading212/portfolio.json", portfolio)
    return len(orders) + len(cash_transactions), len(portfolio)


def _generate_degiro() -> tuple[int, int]:
    # Synthetic representation of the official Dutch transaction export. Empty
    # currency headers intentionally mirror DEGIRO's repeated currency columns.
    transaction_headers = [
        "Datum",
        "Tijd",
        "Product",
        "ISIN",
        "Referentiebeurs",
        "Beurs",
        "Aantal",
        "Koers",
        "",
        "Lokale waarde",
        "",
        "Waarde",
        "",
        "Wisselkoers",
        "Transactiekosten",
        "",
        "Totaal",
        "",
        "Order ID",
    ]
    transaction_rows = [
        [
            "03-07-2026",
            "10:00",
            "Vanguard FTSE All-World UCITS ETF",
            "IE00BK5BQT80",
            "XETR",
            "XETR",
            "2",
            "130.00",
            "EUR",
            "-260.00",
            "EUR",
            "-260.00",
            "EUR",
            "1.0000",
            "-2.00",
            "EUR",
            "-262.00",
            "EUR",
            "DG-20260703-001",
        ],
        [
            "10-07-2026",
            "10:00",
            "ASML Holding NV",
            "NL0010273215",
            "XAMS",
            "XAMS",
            "0.5",
            "680.00",
            "EUR",
            "-340.00",
            "EUR",
            "-340.00",
            "EUR",
            "1.0000",
            "-2.00",
            "EUR",
            "-342.00",
            "EUR",
            "DG-20260710-001",
        ],
        [
            "24-07-2026",
            "10:00",
            "Apple Inc.",
            "US0378331005",
            "XNAS",
            "XNAS",
            "1",
            "210.00",
            "USD",
            "-210.00",
            "USD",
            "-182.61",
            "EUR",
            "1.1500",
            "-1.00",
            "EUR",
            "-183.61",
            "EUR",
            "DG-20260724-001",
        ],
        [
            "31-07-2026",
            "10:00",
            "iShares Core MSCI World UCITS ETF",
            "IE00B4L5Y983",
            "XAMS",
            "XAMS",
            "3",
            "91.00",
            "EUR",
            "-273.00",
            "EUR",
            "-273.00",
            "EUR",
            "1.0000",
            "-2.00",
            "EUR",
            "-275.00",
            "EUR",
            "DG-20260731-001",
        ],
    ]
    _write_csv("degiro/transacties.csv", transaction_headers, transaction_rows)

    statement_headers = [
        "Datum",
        "Tijd",
        "Valutadatum",
        "Product",
        "ISIN",
        "Omschrijving",
        "Wisselkoers",
        "Mutatie",
        "",
        "Saldo",
        "",
        "Order ID",
    ]
    statement_rows = [
        [
            "01-07-2026",
            "09:00",
            "01-07-2026",
            "",
            "",
            "Storting",
            "",
            "350.00",
            "EUR",
            "350.00",
            "EUR",
            "",
        ],
        [
            "17-07-2026",
            "08:00",
            "17-07-2026",
            "Apple Inc.",
            "US0378331005",
            "Dividend",
            "1.1500",
            "0.25",
            "USD",
            "0.25",
            "USD",
            "",
        ],
        [
            "17-07-2026",
            "08:00",
            "17-07-2026",
            "Apple Inc.",
            "US0378331005",
            "Dividendbelasting",
            "1.1500",
            "-0.04",
            "USD",
            "0.21",
            "USD",
            "",
        ],
    ]
    _write_csv(
        "degiro/rekeningoverzicht.csv", statement_headers, statement_rows
    )

    portfolio_headers = [
        "Product",
        "Symbool/ISIN",
        "Aantal",
        "Slotkoers",
        "Valuta",
        "Lokale waarde",
        "Waarde in EUR",
        "GAK",
    ]
    portfolio_rows = [
        [
            "Vanguard FTSE All-World UCITS ETF",
            "IE00BK5BQT80",
            "2",
            "132.00",
            "EUR",
            "264.00",
            "264.00",
            "130.00",
        ],
        [
            "ASML Holding NV",
            "NL0010273215",
            "0.5",
            "700.00",
            "EUR",
            "350.00",
            "350.00",
            "680.00",
        ],
        [
            "Apple Inc.",
            "US0378331005",
            "1",
            "215.00",
            "USD",
            "215.00",
            "186.96",
            "210.00",
        ],
        [
            "iShares Core MSCI World UCITS ETF",
            "IE00B4L5Y983",
            "3",
            "92.00",
            "EUR",
            "276.00",
            "276.00",
            "91.00",
        ],
    ]
    _write_csv("degiro/portefeuille.csv", portfolio_headers, portfolio_rows)
    return len(transaction_rows) + 1, len(portfolio_rows)


def _date_coverage() -> list[str]:
    first = date(2026, 7, 1)
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range(31)
    ]


def main() -> None:
    bunq_count = _generate_bunq()
    t212_events, t212_holdings = _generate_trading212()
    degiro_events, degiro_holdings = _generate_degiro()
    manifest = {
        "dataset": "finance-sync-staging-2026-07",
        "synthetic": True,
        "period": {"from": "2026-07-01", "to": "2026-07-31"},
        "timezone": "Europe/Amsterdam",
        "expected": {
            "bunq": {
                "accounts": 1,
                "transactions": bunq_count,
                "daily_date_coverage": _date_coverage(),
            },
            "degiro_pension": {
                "investment_events": degiro_events,
                "trade_rows": 4,
                "dividend_rows": 1,
                "tax_rows": 1,
                "holdings": degiro_holdings,
                "security_types": ["stock", "etf"],
            },
            "trading212": {
                "investment_events": t212_events,
                "order_rows": 4,
                "dividend_rows": 1,
                "holdings": t212_holdings,
                "security_types": ["stock", "etf"],
            },
        },
        "routes": {
            "bunq": {
                "POST /v1/session-server": "bunq/session-server.json",
                "GET /v1/user/9900001/monetary-account": (
                    "bunq/monetary-accounts.json"
                ),
                "GET /v1/monetary-account/9100001/payment": (
                    "bunq/payments-account-9100001.json"
                ),
            },
            "trading212": {
                "GET /api/v0/equity/account/info": (
                    "trading212/account-info.json"
                ),
                "GET /api/v0/equity/account/cash": (
                    "trading212/account-cash.json"
                ),
                "GET /api/v0/equity/history/orders": (
                    "trading212/order-history.json"
                ),
                "GET /api/v0/equity/history/transactions": (
                    "trading212/transaction-history.json"
                ),
                "GET /api/v0/equity/portfolio": "trading212/portfolio.json",
            },
        },
    }
    _write_json("manifest.json", manifest)


if __name__ == "__main__":
    main()
