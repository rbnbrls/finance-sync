"""Example bunq API responses for use in test fixtures.

All responses are anonymised — real account IDs, IBANs, and amounts
have been replaced with synthetic values.  The JSON shapes match the
bunq v1 API responses exactly.

These are used by :class:`tests.connectors.test_bunq.BunqApiMockTransport`
to simulate the bunq API without network calls.
"""

from __future__ import annotations

from typing import Any

# ── Session-Server ──────────────────────────────────────────────────────

SESSION_SERVER_RESPONSE: dict[str, Any] = {
    "Response": [
        {
            "Token": {
                "id": 12345,
                "created": "2025-01-01 00:00:00.123456",
                "updated": "2025-01-01 00:00:00.123456",
                "token": "bunq_session_token_test_abcdef123456",
            }
        },
        {
            "UserPerson": {
                "id": 54321,
                "created": "2024-06-01 10:00:00.000000",
                "updated": "2025-06-15 08:30:00.000000",
                "public_uuid": "test-0000-0000-0000-000000000001",
                "display_name": "Test User",
            }
        },
    ],
    "Pagination": {"future_url": None},
}

# ── Monetary Accounts ──────────────────────────────────────────────────

# An IBAN current account (MonetaryAccountBank)
MONETARY_ACCOUNT_BANK: dict[str, Any] = {
    "MonetaryAccountBank": {
        "id": 1000001,
        "created": "2024-01-15 09:00:00.000000",
        "updated": "2025-06-20 12:00:00.000000",
        "description": "Main Checking",
        "status": "ACTIVE",
        "sub_type": "CURRENT",
        "balance": {"value": "1520.45", "currency": "EUR"},
        "alias": [
            {
                "type": "IBAN",
                "value": "NL99BUNQ1234567890",
                "name": "T. User",
            }
        ],
    }
}

# A savings account (MonetaryAccountSavings)
MONETARY_ACCOUNT_SAVINGS: dict[str, Any] = {
    "MonetaryAccountSavings": {
        "id": 1000002,
        "created": "2024-03-01 10:00:00.000000",
        "updated": "2025-06-19 18:00:00.000000",
        "description": "Emergency Fund",
        "status": "ACTIVE",
        "balance": {"value": "5000.00", "currency": "EUR"},
        "alias": [
            {
                "type": "IBAN",
                "value": "NL99BUNQ0987654321",
                "name": "T. User Savings",
            }
        ],
    }
}

# A savings goal account (MonetaryAccountSavings with goal fields)
SAVINGS_GOAL_ACCOUNT: dict[str, Any] = {
    "MonetaryAccountSavings": {
        "id": 1000003,
        "created": "2025-01-10 08:00:00.000000",
        "updated": "2025-06-18 20:00:00.000000",
        "description": "Summer Holiday 2026",
        "status": "ACTIVE",
        "balance": {"value": "1200.00", "currency": "EUR"},
        "alias": [
            {
                "type": "IBAN",
                "value": "NL99BUNQ5555555555",
                "name": "T. User Holiday",
            }
        ],
        "savings_goal": {
            "amount_target": {"value": "3000.00", "currency": "EUR"}
        },
    }
}

MONETARY_ACCOUNTS_RESPONSE: dict[str, Any] = {
    "Response": [
        MONETARY_ACCOUNT_BANK,
        MONETARY_ACCOUNT_SAVINGS,
        SAVINGS_GOAL_ACCOUNT,
    ],
    "Pagination": {"future_url": None},
}

# ── Payments (Transactions) ────────────────────────────────────────────

PAYMENT_PURCHASE: dict[str, Any] = {
    "Payment": {
        "id": 2000001,
        "created": "2025-06-15 14:30:00.123456",
        "updated": "2025-06-15 14:35:00.000000",
        "monetary_account_id": 1000001,
        "amount": {"value": "-42.50", "currency": "EUR"},
        "description": "Coffee shop",
        "type": "PAYMENT",
        "status": "ACCEPTED",
        "sub_type": None,
        "counterparty_alias": {
            "type": "IBAN",
            "value": "NL00OTHER0123456789",
            "name": "Coffee Shop B.V.",
        },
        "attachment": [],
    }
}

PAYMENT_TRANSFER: dict[str, Any] = {
    "Payment": {
        "id": 2000002,
        "created": "2025-06-14 09:00:00.000000",
        "updated": "2025-06-14 09:05:00.000000",
        "monetary_account_id": 1000001,
        "amount": {"value": "-200.00", "currency": "EUR"},
        "description": "Transfer to savings",
        "type": "TRANSFER",
        "status": "ACCEPTED",
        "sub_type": None,
        "counterparty_alias": {
            "type": "IBAN",
            "value": "NL99BUNQ0987654321",
            "name": "T. User Savings",
        },
        "attachment": [],
    }
}

PAYMENT_INTEREST: dict[str, Any] = {
    "Payment": {
        "id": 2000003,
        "created": "2025-06-01 00:00:00.000000",
        "updated": "2025-06-01 00:00:00.000000",
        "monetary_account_id": 1000002,
        "amount": {"value": "0.87", "currency": "EUR"},
        "description": "Interest",
        "type": "INTEREST",
        "status": "ACCEPTED",
        "sub_type": None,
        "counterparty_alias": None,
        "attachment": [],
    }
}

PAYMENT_PENDING: dict[str, Any] = {
    "Payment": {
        "id": 2000004,
        "created": "2025-06-20 18:00:00.000000",
        "updated": "2025-06-20 18:00:00.000000",
        "monetary_account_id": 1000001,
        "amount": {"value": "-15.99", "currency": "EUR"},
        "description": "Pending subscription",
        "type": "BILLING",
        "status": "PENDING",
        "sub_type": None,
        "counterparty_alias": {
            "type": "IBAN",
            "value": "NL00SUBS9876543210",
            "name": "Subscription Co.",
        },
        "attachment": [],
    }
}

# Payments for account 1000001 (Main Checking)
PAYMENTS_ACCOUNT_1000001: dict[str, Any] = {
    "Response": [
        PAYMENT_PURCHASE,
        PAYMENT_TRANSFER,
        PAYMENT_PENDING,
    ],
    "Pagination": {"future_url": None},
}

# Payments for account 1000002 (Emergency Fund)
PAYMENTS_ACCOUNT_1000002: dict[str, Any] = {
    "Response": [
        PAYMENT_INTEREST,
    ],
    "Pagination": {"future_url": None},
}

# Payments for account 1000003 (Holiday Savings Goal — no payments)
PAYMENTS_ACCOUNT_1000003: dict[str, Any] = {
    "Response": [],
    "Pagination": {"future_url": None},
}

# ── Paginated response (2 pages) ───────────────────────────────────────

PAGE_1_RESPONSE: dict[str, Any] = {
    "Response": [
        {
            "MonetaryAccountBank": {
                "id": 1000001,
                "description": "Main Checking",
                "status": "ACTIVE",
                "balance": {"value": "1520.45", "currency": "EUR"},
                "alias": [{"type": "IBAN", "value": "NL99BUNQ1234567890"}],
            }
        },
    ],
    "Pagination": {
        "future_url": "/v1/user/54321/monetary-account?count=1&newer_id=1000001"
    },
}

PAGE_2_RESPONSE: dict[str, Any] = {
    "Response": [
        {
            "MonetaryAccountSavings": {
                "id": 1000002,
                "description": "Emergency Fund",
                "status": "ACTIVE",
                "balance": {"value": "5000.00", "currency": "EUR"},
                "alias": [{"type": "IBAN", "value": "NL99BUNQ0987654321"}],
            }
        },
    ],
    "Pagination": {"future_url": None},
}
