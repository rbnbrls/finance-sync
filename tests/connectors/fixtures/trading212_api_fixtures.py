"""Example Trading212 API responses for use in test fixtures.

All responses are anonymised — real account IDs, amounts, and order
identifiers have been replaced with synthetic values.  The JSON shapes
match the Trading212 v0 API responses exactly.

These are used by
:class:`tests.connectors.trading212.conftest.Trading212MockTransport`
to simulate the Trading212 API without network calls.
"""

from __future__ import annotations

from typing import Any

# ── Account / Cash ──────────────────────────────────────────────────────

ACCOUNT_CASH_RESPONSE: dict[str, Any] = {
    "free": 10000.50,
    "invested": 25500.00,
    "result": 3200.00,
    "blocked": 0.0,
    "pending": 0.0,
    "pieCash": 0.0,
    "currencyCode": "EUR",
}

ACCOUNT_INFO_RESPONSE: dict[str, Any] = {
    "id": 12345678,
    "currencyCode": "EUR",
}

# ── Portfolio ───────────────────────────────────────────────────────────

PORTFOLIO_RESPONSE: list[dict[str, Any]] = [
    {
        "ticker": "AAPL",
        "quantity": 10.0,
        "averagePrice": 175.50,
        "currentPrice": 180.00,
        "initialFillDate": "2024-01-15T10:00:00.000Z",
        "frontend": "STOCKS",
        "maxBuy": 5000.00,
        "maxSell": 10.0,
        "pieQuantity": 0.0,
        "ppl": {
            "ppl": 45.00,
            "percentage": 2.56,
            "investment": 1755.00,
            "currencyCode": "EUR",
        },
    },
    {
        "ticker": "TSLA",
        "quantity": 5.0,
        "averagePrice": 245.00,
        "currentPrice": 260.00,
        "initialFillDate": "2024-03-01T09:30:00.000Z",
        "frontend": "STOCKS",
        "maxBuy": 3000.00,
        "maxSell": 5.0,
        "pieQuantity": 0.0,
        "ppl": {
            "ppl": 75.00,
            "percentage": 6.12,
            "investment": 1225.00,
            "currencyCode": "EUR",
        },
    },
    {
        "ticker": "VWCE.DE",
        "quantity": 50.0,
        "averagePrice": 125.00,
        "currentPrice": 130.50,
        "initialFillDate": "2024-06-10T08:00:00.000Z",
        "frontend": "ETF",
        "maxBuy": 8000.00,
        "maxSell": 50.0,
        "pieQuantity": 50.0,
        "ppl": {
            "ppl": 275.00,
            "percentage": 4.40,
            "investment": 6250.00,
            "currencyCode": "EUR",
        },
    },
]

# ── Orders (Buy/Sell history) ──────────────────────────────────────────

ORDER_BUY_AAPL: dict[str, Any] = {
    "id": 10000001,
    "ticker": "AAPL",
    "type": "MARKET",
    "side": "BUY",
    "quantity": 10.0,
    "filledQuantity": 10.0,
    "price": None,
    "filledPrice": 175.50,
    "total": 1755.00,
    "status": "FILLED",
    "creationTime": "2024-01-15T10:00:00.000Z",
    "filledTime": "2024-01-15T10:00:30.000Z",
    "currencyCode": "EUR",
    "tax": 0.0,
    "stampDuty": 0.0,
    "executionVenue": "SMART",
    "frontend": "STOCKS",
}

ORDER_SELL_TSLA: dict[str, Any] = {
    "id": 10000002,
    "ticker": "TSLA",
    "type": "LIMIT",
    "side": "SELL",
    "quantity": 2.0,
    "filledQuantity": 2.0,
    "price": 270.00,
    "filledPrice": 270.00,
    "total": 540.00,
    "status": "FILLED",
    "creationTime": "2024-06-20T14:00:00.000Z",
    "filledTime": "2024-06-20T14:00:15.000Z",
    "currencyCode": "EUR",
    "tax": 0.0,
    "stampDuty": 0.0,
    "executionVenue": "SMART",
    "frontend": "STOCKS",
}

ORDER_BUY_VWCE: dict[str, Any] = {
    "id": 10000003,
    "ticker": "VWCE.DE",
    "type": "MARKET",
    "side": "BUY",
    "quantity": 50.0,
    "filledQuantity": 50.0,
    "price": None,
    "filledPrice": 125.00,
    "total": 6250.00,
    "status": "FILLED",
    "creationTime": "2024-06-10T08:00:00.000Z",
    "filledTime": "2024-06-10T08:00:45.000Z",
    "currencyCode": "EUR",
    "tax": 0.0,
    "stampDuty": 0.0,
    "executionVenue": "SMART",
    "frontend": "ETF",
}

ORDER_PENDING: dict[str, Any] = {
    "id": 10000004,
    "ticker": "AAPL",
    "type": "LIMIT",
    "side": "BUY",
    "quantity": 5.0,
    "filledQuantity": 0.0,
    "price": 160.00,
    "filledPrice": None,
    "total": 800.00,
    "status": "PENDING",
    "creationTime": "2025-06-20T18:00:00.000Z",
    "filledTime": None,
    "currencyCode": "EUR",
    "tax": 0.0,
    "stampDuty": 0.0,
    "executionVenue": "SMART",
    "frontend": "STOCKS",
}

ORDER_HISTORY_RESPONSE: dict[str, Any] = {
    "items": [
        ORDER_BUY_AAPL,
        ORDER_SELL_TSLA,
        ORDER_BUY_VWCE,
        ORDER_PENDING,
    ],
    "nextPagePath": None,
}

ORDER_HISTORY_PAGE_1: dict[str, Any] = {
    "items": [
        ORDER_BUY_AAPL,
        ORDER_SELL_TSLA,
    ],
    "nextPagePath": (
        "/api/v0/equity/history/orders?cursor=page2_cursor&limit=100"
    ),
}

ORDER_HISTORY_PAGE_2: dict[str, Any] = {
    "items": [
        ORDER_BUY_VWCE,
        ORDER_PENDING,
    ],
    "nextPagePath": None,
}

# ── Cash Transactions (Dividends, Deposits, etc.) ──────────────────────

DIVIDEND_AAPL: dict[str, Any] = {
    "id": 20000001,
    "type": "DIVIDEND",
    "dateTime": "2024-05-15T00:00:00.000Z",
    "amount": 15.00,
    "currencyCode": "EUR",
    "reference": "AAPL Dividend May 2024",
    "ticker": "AAPL",
}

DIVIDEND_VWCE: dict[str, Any] = {
    "id": 20000002,
    "type": "DIVIDEND",
    "dateTime": "2024-06-20T00:00:00.000Z",
    "amount": 62.50,
    "currencyCode": "EUR",
    "reference": "VWCE Distribution",
    "ticker": "VWCE.DE",
}

DEPOSIT_1: dict[str, Any] = {
    "id": 20000003,
    "type": "DEPOSIT",
    "dateTime": "2024-01-10T09:00:00.000Z",
    "amount": 5000.00,
    "currencyCode": "EUR",
    "reference": "SEPA Deposit",
    "ticker": None,
}

WITHDRAWAL_1: dict[str, Any] = {
    "id": 20000004,
    "type": "WITHDRAWAL",
    "dateTime": "2024-12-01T10:00:00.000Z",
    "amount": 1000.00,
    "currencyCode": "EUR",
    "reference": "Withdrawal to IBAN",
    "ticker": None,
}

INTEREST_1: dict[str, Any] = {
    "id": 20000005,
    "type": "INTEREST",
    "dateTime": "2024-12-31T23:00:00.000Z",
    "amount": 12.34,
    "currencyCode": "EUR",
    "reference": "Interest on uninvested cash",
    "ticker": None,
}

FEE_1: dict[str, Any] = {
    "id": 20000006,
    "type": "FEE",
    "dateTime": "2024-06-01T00:00:00.000Z",
    "amount": 2.50,
    "currencyCode": "EUR",
    "reference": "Monthly account fee",
    "ticker": None,
}

TRANSACTION_HISTORY_RESPONSE: dict[str, Any] = {
    "items": [
        DIVIDEND_AAPL,
        DIVIDEND_VWCE,
        DEPOSIT_1,
        WITHDRAWAL_1,
        INTEREST_1,
        FEE_1,
    ],
    "nextPagePath": None,
}

TRANSACTION_HISTORY_PAGE_1: dict[str, Any] = {
    "items": [
        DIVIDEND_AAPL,
        DIVIDEND_VWCE,
        DEPOSIT_1,
    ],
    "nextPagePath": (
        "/api/v0/equity/history/transactions?cursor=txn_page2&limit=100"
    ),
}

TRANSACTION_HISTORY_PAGE_2: dict[str, Any] = {
    "items": [
        WITHDRAWAL_1,
        INTEREST_1,
        FEE_1,
    ],
    "nextPagePath": None,
}
