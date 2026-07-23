"""Provider/consumer fixtures for the Actual Budget exporter contract tests.

These fixtures simulate the "consumer" side of the export contract —
finance-sync accounts and transactions that the Actual Budget exporter
would consume from the database.  They mirror realistic data that would
flow through a bunq or Trading212 connector.

All values are synthetic for deterministic test assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

# ═══════════════════════════════════════════════════════════════════════
# Consumer fixtures — finance-sync account & transaction records
# ═══════════════════════════════════════════════════════════════════════


def make_ab_account(
    *,
    account_id: str | None = None,
    name: str = "Checking Account",
    provider_key: str = "bunq",
    account_type: str = "checking",
    currency_code: str = "EUR",
) -> MagicMock:
    """Build a consumer-side Account ORM instance for AB export."""
    acct = MagicMock()
    acct.id = account_id or str(uuid4())
    acct.tenant_id = "tenant_ab_contract"
    acct.provider_key = provider_key
    acct.external_account_id = f"ext_{acct.id[:8]}"
    acct.name = name
    acct.account_type = account_type
    acct.currency_code = currency_code
    acct.is_active = True
    return acct


def make_ab_transaction(
    *,
    account_id: str | None = None,
    txn_type: str = "payment",
    amount: str = "-42.50",
    currency: str = "EUR",
    description: str = "Coffee Shop",
    status: str = "booked",
    security_id: str | None = None,
    occurred_at: datetime | None = None,
    amount_in_base: str | None = None,
    base_currency: str | None = None,
    fx_rate: str | None = None,
) -> MagicMock:
    """Build a consumer-side Transaction ORM instance for AB export."""
    txn = MagicMock()
    txn.id = str(uuid4())
    txn.tenant_id = "tenant_ab_contract"
    txn.account_id = account_id or str(uuid4())
    txn.provider_key = "bunq"
    txn.external_transaction_id = f"ext_txn_{uuid4().hex[:8]}"
    txn.amount = Decimal(amount)
    txn.currency_code = currency
    txn.occurred_at = occurred_at or datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
    txn.booked_at = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)
    txn.transaction_type = txn_type
    txn.description = description
    txn.status = status
    txn.revision = 1
    txn.security_id = security_id
    txn.provider_fingerprint = f"fp_{uuid4().hex[:12]}"
    txn.amount_in_base = Decimal(amount_in_base) if amount_in_base else None
    txn.base_currency_code = base_currency
    txn.fx_rate = Decimal(fx_rate) if fx_rate else None
    return txn


# ═══════════════════════════════════════════════════════════════════════
# Pre-built fixture sets
# ═══════════════════════════════════════════════════════════════════════

AB_ACCOUNT_CHECKING = make_ab_account(
    name="Main Checking",
    account_type="checking",
    currency_code="EUR",
)

AB_ACCOUNT_SAVINGS = make_ab_account(
    name="Savings Account",
    account_type="savings",
    currency_code="EUR",
)

AB_ACCOUNT_INVESTMENT = make_ab_account(
    name="Brokerage",
    account_type="brokerage",
    currency_code="USD",
    provider_key="trading212",
)

AB_TRANSACTION_PAYMENT = make_ab_transaction(
    txn_type="payment",
    amount="-42.50",
    description="Coffee Shop",
    account_id=AB_ACCOUNT_CHECKING.id,
)

AB_TRANSACTION_DEPOSIT = make_ab_transaction(
    txn_type="deposit",
    amount="1500.00",
    description="Salary deposit",
    account_id=AB_ACCOUNT_CHECKING.id,
)

AB_TRANSACTION_WITHDRAWAL = make_ab_transaction(
    txn_type="withdrawal",
    amount="-200.00",
    description="ATM withdrawal",
    account_id=AB_ACCOUNT_CHECKING.id,
)

AB_TRANSACTION_FEE = make_ab_transaction(
    txn_type="fee",
    amount="-5.99",
    description="Monthly account fee",
    account_id=AB_ACCOUNT_CHECKING.id,
)

AB_TRANSACTION_INTEREST = make_ab_transaction(
    txn_type="interest",
    amount="3.42",
    description="Interest payment",
    account_id=AB_ACCOUNT_SAVINGS.id,
)

AB_TRANSACTION_FX = make_ab_transaction(
    txn_type="payment",
    amount="-100.00",
    currency="USD",
    description="USD Payment",
    account_id=AB_ACCOUNT_INVESTMENT.id,
    amount_in_base="-92.50",
    base_currency="EUR",
    fx_rate="0.9250",
)

AB_TRANSACTION_PENDING = make_ab_transaction(
    txn_type="payment",
    amount="-15.99",
    description="Pending subscription",
    account_id=AB_ACCOUNT_CHECKING.id,
    status="pending",
)

AB_TRANSACTION_TRANSFER = make_ab_transaction(
    txn_type="transfer",
    amount="-500.00",
    description="Transfer to savings",
    account_id=AB_ACCOUNT_CHECKING.id,
)

TRANSACTION_MAP_TEST_CASES: list[dict[str, Any]] = [
    {
        "txn": AB_TRANSACTION_PAYMENT,
        "expected_payee": "Coffee Shop",
        "expected_imported_id_prefix": "fs_",
        "expected_amount_cents": -4250,
    },
    {
        "txn": AB_TRANSACTION_DEPOSIT,
        "expected_payee": "Salary deposit",
        "expected_amount_cents": 150000,
    },
    {
        "txn": AB_TRANSACTION_FX,
        "expected_notes_contains": "FX",
        "expected_amount_cents": -10000,
    },
    {
        "txn": AB_TRANSACTION_PENDING,
        "expected_cleared": False,
        "expected_amount_cents": -1599,
    },
]
