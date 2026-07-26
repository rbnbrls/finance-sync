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


# ── Schedule Payment fixtures ────────────────────────────────────────────

SCHEDULE_PAYMENT_MONTHLY: dict[str, Any] = {
    "SchedulePayment": {
        "id": 3000001,
        "created": "2025-01-01 10:00:00.000000",
        "updated": "2025-06-20 12:00:00.000000",
        "payment": {
            "id": 4000001,
            "amount": {"value": "-150.00", "currency": "EUR"},
            "description": "Monthly rent",
            "counterparty_alias": {
                "type": "IBAN",
                "value": "NL00LANDLORD1234567",
                "name": "Landlord B.V.",
            },
        },
        "schedule": {
            "time_unit": "MONTHLY",
            "interval": 1,
            "start": {"value": "2025-01-01 00:00:00.000000"},
            "end": {"value": "2026-01-01 00:00:00.000000"},
            "status": "ACTIVE",
            "next_execution": {"value": "2025-07-01 00:00:00.000000"},
        },
        "schedule_instance": [
            {"id": 5000001, "created": "2025-01-01 10:00:00.000000"},
            {"id": 5000002, "created": "2025-02-01 10:00:00.000000"},
            {"id": 5000003, "created": "2025-03-01 10:00:00.000000"},
            {"id": 5000004, "created": "2025-04-01 10:00:00.000000"},
            {"id": 5000005, "created": "2025-05-01 10:00:00.000000"},
            {"id": 5000006, "created": "2025-06-01 10:00:00.000000"},
        ],
    }
}

SCHEDULE_PAYMENT_WEEKLY: dict[str, Any] = {
    "SchedulePayment": {
        "id": 3000002,
        "created": "2025-03-15 08:00:00.000000",
        "updated": "2025-06-18 16:00:00.000000",
        "payment": {
            "id": 4000002,
            "amount": {"value": "-25.00", "currency": "EUR"},
            "description": "Weekly subscription",
            "counterparty_alias": {
                "type": "IBAN",
                "value": "NL00SUBS9876543210",
                "name": "Streaming Co.",
            },
        },
        "schedule": {
            "time_unit": "WEEKLY",
            "interval": 1,
            "start": {"value": "2025-03-15 08:00:00.000000"},
            "status": "ACTIVE",
            "next_execution": {"value": "2025-06-22 08:00:00.000000"},
        },
        "schedule_instance": [],
    }
}

SCHEDULES_ACCOUNT_1000001: dict[str, Any] = {
    "Response": [
        SCHEDULE_PAYMENT_MONTHLY,
        SCHEDULE_PAYMENT_WEEKLY,
    ],
    "Pagination": {"future_url": None},
}

SCHEDULES_ACCOUNT_1000002: dict[str, Any] = {
    "Response": [],
    "Pagination": {"future_url": None},
}

SCHEDULES_ACCOUNT_1000003: dict[str, Any] = {
    "Response": [],
    "Pagination": {"future_url": None},
}


# ── Card fixtures ────────────────────────────────────────────────────────

CARDS_RESPONSE: dict[str, Any] = {
    "Response": [
        {
            "Card": {
                "id": 7000001,
                "created": "2025-01-15 09:00:00.000000",
                "updated": "2025-06-20 12:00:00.000000",
                "type": "DEBIT_CARD",
                "status": "ACTIVE",
                "name": "My Debit Card",
                "last_four": "1234",
            }
        },
        {
            "Card": {
                "id": 7000002,
                "created": "2025-04-01 10:00:00.000000",
                "updated": "2025-06-19 18:00:00.000000",
                "type": "CREDIT_CARD",
                "status": "ACTIVE",
                "name": "My Credit Card",
                "last_four": "5678",
            }
        },
    ],
    "Pagination": {"future_url": None},
}

CARD_PAYMENT_AUTHORIZATION: dict[str, Any] = {
    "CardPayment": {
        "id": 8000001,
        "created": "2025-06-20 14:30:00.123456",
        "updated": "2025-06-20 14:30:00.123456",
        "amount": {"value": "-42.50", "currency": "EUR"},
        "merchant_name": "Supermarket B.V.",
        "merchant_city": "Amsterdam",
        "merchant_country": "NL",
        "mcc": "5411",
        "card": {"id": 7000001, "type": "DEBIT_CARD"},
        "authorisation_status": "AUTHORISATION",
        "description": "Grocery shopping",
    }
}

CARD_PAYMENT_SETTLEMENT: dict[str, Any] = {
    "CardPayment": {
        "id": 8000002,
        "created": "2025-06-18 10:00:00.000000",
        "updated": "2025-06-19 08:00:00.000000",
        "amount": {"value": "-89.99", "currency": "EUR"},
        "merchant_name": "Online Store",
        "merchant_city": "Utrecht",
        "merchant_country": "NL",
        "mcc": "5732",
        "card": {"id": 7000001, "type": "DEBIT_CARD"},
        "authorisation_status": "SETTLEMENT",
        "description": "Electronics purchase",
    }
}

CARD_PAYMENT_REFUND: dict[str, Any] = {
    "CardPayment": {
        "id": 8000003,
        "created": "2025-06-17 15:00:00.000000",
        "updated": "2025-06-18 09:00:00.000000",
        "amount": {"value": "15.99", "currency": "EUR"},
        "merchant_name": "Online Store",
        "merchant_city": "Utrecht",
        "merchant_country": "NL",
        "mcc": "5732",
        "card": {"id": 7000001, "type": "DEBIT_CARD"},
        "authorisation_status": "REFUND",
        "description": "Refund for returned item",
    }
}

CARD_PAYMENTS_CARD_7000001: dict[str, Any] = {
    "Response": [
        CARD_PAYMENT_AUTHORIZATION,
        CARD_PAYMENT_SETTLEMENT,
        CARD_PAYMENT_REFUND,
    ],
    "Pagination": {"future_url": None},
}

CARD_PAYMENTS_CARD_7000002: dict[str, Any] = {
    "Response": [],
    "Pagination": {"future_url": None},
}
