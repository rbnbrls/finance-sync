"""YNAB API response fixtures for contract tests.

These are simplified but realistic YNAB API responses used by
the mock HTTP transport in ``conftest.py``.
"""

# ── GET /budgets ─────────────────────────────────────────────────────────

BUDGETS_RESPONSE = {
    "data": {
        "budgets": [
            {
                "id": "ynab_budget_001",
                "name": "My Budget",
                "last_modified_on": "2025-06-01T10:00:00Z",
                "first_month": "2025-01-01",
                "last_month": "2025-06-01",
                "date_format": {"format": "YYYY-MM-DD"},
                "currency_format": {
                    "iso_code": "EUR",
                    "decimal_digits": 2,
                    "decimal_separator": ".",
                    "symbol_separator": ",",
                    "display_symbol": "\u20ac",
                    "currency_symbol": "\u20ac",
                },
            },
        ],
        "server_knowledge": 42,
    }
}

# ── GET /budgets/{id}/accounts ──────────────────────────────────────────

BUDGET_ACCOUNTS_RESPONSE = {
    "data": {
        "accounts": [
            {
                "id": "ynab_acc_checking_01",
                "name": "Checking Account",
                "type": "checking",
                "on_budget": True,
                "closed": False,
                "deleted": False,
                "balance": 1520450,  # 1520.45 in milliunits
                "cleared_balance": 1500000,
                "uncleared_balance": 20450,
            },
            {
                "id": "ynab_acc_savings_01",
                "name": "Emergency Fund",
                "type": "savings",
                "on_budget": True,
                "closed": False,
                "deleted": False,
                "balance": 50000000,  # 50000.00 in milliunits
                "cleared_balance": 50000000,
                "uncleared_balance": 0,
            },
            {
                "id": "ynab_acc_credit_01",
                "name": "Rewards Credit Card",
                "type": "creditCard",
                "on_budget": True,
                "closed": False,
                "deleted": False,
                "balance": -450250,  # -450.25 in milliunits
                "cleared_balance": -450250,
                "uncleared_balance": 0,
            },
        ],
        "server_knowledge": 42,
    }
}

# ── GET /budgets/{id}/transactions ──────────────────────────────────────

BUDGET_TRANSACTIONS_RESPONSE = {
    "data": {
        "transactions": [
            {
                "id": "ynab_txn_001",
                "date": "2025-06-15",
                "amount": 42500,  # 42.50 outflow -> -42.50
                "memo": "Coffee shop",
                "cleared": "cleared",
                "approved": True,
                "flag_color": None,
                "account_id": "ynab_acc_checking_01",
                "category_id": "cat_food_001",
                "category_name": "Food & Dining",
                "transfer_account_id": None,
                "payee_name": "Starbucks",
                "import_id": None,
                "deleted": False,
                "matched_transaction_id": None,
                "source": "direct_import",
                "subtransactions": [],
            },
            {
                "id": "ynab_txn_002",
                "date": "2025-06-14",
                "amount": -120000,  # -120.00 inflow -> +120.00
                "memo": "Freelance payment",
                "cleared": "cleared",
                "approved": True,
                "flag_color": "green",
                "account_id": "ynab_acc_checking_01",
                "category_id": "cat_income_001",
                "category_name": "Income: Freelance",
                "transfer_account_id": None,
                "payee_name": "Client Corp",
                "import_id": None,
                "deleted": False,
                "matched_transaction_id": None,
                "source": "direct_import",
                "subtransactions": [],
            },
            {
                "id": "ynab_txn_003",
                "date": "2025-06-13",
                "amount": 1500,  # 1.50 fee
                "memo": "Service fee",
                "cleared": "cleared",
                "approved": True,
                "flag_color": "red",
                "account_id": "ynab_acc_checking_01",
                "category_id": "cat_fees_001",
                "category_name": "Bank Fees",
                "transfer_account_id": None,
                "payee_name": "Bank",
                "import_id": None,
                "deleted": False,
                "matched_transaction_id": None,
                "source": "direct_import",
                "subtransactions": [],
            },
            {
                "id": "ynab_txn_004",
                "date": "2025-06-12",
                "amount": 100000,  # 100.00 transfer
                "memo": "To savings",
                "cleared": "cleared",
                "approved": True,
                "flag_color": None,
                "account_id": "ynab_acc_checking_01",
                "category_id": None,
                "category_name": None,
                "transfer_account_id": "ynab_acc_savings_01",
                "payee_name": "Transfer : Emergency Fund",
                "import_id": None,
                "deleted": False,
                "matched_transaction_id": None,
                "source": "direct_import",
                "subtransactions": [],
            },
            {
                "id": "ynab_txn_005",
                "date": "2025-06-11",
                "amount": 2500,
                "memo": "Interest payment",
                "cleared": "uncleared",
                "approved": False,
                "flag_color": "purple",
                "account_id": "ynab_acc_savings_01",
                "category_id": "cat_interest_001",
                "category_name": "Interest Income",
                "transfer_account_id": None,
                "payee_name": "Bank",
                "import_id": None,
                "deleted": False,
                "matched_transaction_id": None,
                "source": "direct_import",
                "subtransactions": [],
            },
        ],
        "server_knowledge": 43,
    }
}

# ── Transaction parsed from fixture data ─────────────────────────────────

# Pre-parsed transaction amounts for assertions (YNAB milliunits → Decimal)
# YNAB convention: positive = outflow, negative = inflow
# finance-sync convention: positive = inflow, negative = outflow
FIXTURE_TXN_AMOUNTS = {
    "ynab_txn_001": "-42.50",  # outflow → negative
    "ynab_txn_002": "120.00",  # inflow → positive
    "ynab_txn_003": "-1.50",  # fee outflow → negative
    "ynab_txn_005": "-2.50",  # interest outflow → negative
}
