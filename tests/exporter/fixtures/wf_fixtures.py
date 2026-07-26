"""Provider/consumer fixtures for the Wealthfolio exporter contract tests.

These fixtures simulate both the consumer side (finance-sync accounts,
transactions, holdings, securities) and the provider side (Wealthfolio
CSV import format expectations).

All values are synthetic for deterministic test assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

# ═══════════════════════════════════════════════════════════════════════
# Consumer fixtures — securities
# ═══════════════════════════════════════════════════════════════════════


def make_security(
    *,
    ticker: str = "AAPL",
    isin: str = "US0378331005",
    figi: str = "BBG000B9XRY4",
    name: str = "Apple Inc.",
    security_type: str = "stock",
    currency_code: str = "USD",
) -> MagicMock:
    """Build a Security ORM instance."""
    sec = MagicMock()
    sec.id = str(uuid4())
    sec.isin = isin
    sec.figi = figi
    sec.cusip = "037833100"
    sec.ticker = ticker
    sec.name = name
    sec.security_type = security_type
    sec.currency_code = currency_code
    return sec


def make_wf_account(
    *,
    name: str = "Brokerage Account",
    account_type: str = "brokerage",
    provider_key: str = "trading212",
    currency_code: str = "EUR",
) -> MagicMock:
    """Build a consumer-side Account ORM instance for WF export."""
    acct = MagicMock()
    acct.id = str(uuid4())
    acct.tenant_id = "tenant_wf_contract"
    acct.provider_key = provider_key
    acct.external_account_id = f"ext_{acct.id[:8]}"
    acct.name = name
    acct.account_type = account_type
    acct.currency_code = currency_code
    acct.is_active = True
    return acct


def make_wf_transaction(
    *,
    account_id: str | None = None,
    security_id: str | None = None,
    txn_type: str = "purchase",
    amount: str = "-1505.00",
    currency: str = "USD",
    description: str = "Buy 10 AAPL",
    status: str = "booked",
    occurred_at: datetime | None = None,
    amount_in_base: str | None = None,
    base_currency: str | None = None,
    fx_rate: str | None = None,
) -> MagicMock:
    """Build a consumer-side Transaction ORM instance for WF export."""
    txn = MagicMock()
    txn.id = str(uuid4())
    txn.tenant_id = "tenant_wf_contract"
    txn.account_id = account_id or str(uuid4())
    txn.security_id = security_id
    txn.provider_key = "trading212"
    txn.external_transaction_id = f"ext_{uuid4().hex[:8]}"
    txn.amount = Decimal(amount)
    txn.currency_code = currency
    txn.occurred_at = occurred_at or datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
    txn.booked_at = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)
    txn.transaction_type = txn_type
    txn.description = description
    txn.status = status
    txn.revision = 1
    txn.provider_fingerprint = f"fp_{uuid4().hex[:12]}"
    txn.amount_in_base = Decimal(amount_in_base) if amount_in_base else None
    txn.base_currency_code = base_currency
    txn.fx_rate = Decimal(fx_rate) if fx_rate else None
    return txn


def make_wf_holding(
    *,
    account_id: str | None = None,
    security_id: str | None = None,
    quantity: str = "50",
    cost_basis: str = "8574.00",
    market_value: str = "9500.00",
    price: str = "190.00",
    currency: str = "USD",
) -> MagicMock:
    """Build a consumer-side Holding ORM instance for WF export."""
    holding = MagicMock()
    holding.id = str(uuid4())
    holding.tenant_id = "tenant_wf_contract"
    holding.account_id = account_id or str(uuid4())
    holding.security_id = security_id or str(uuid4())
    holding.observed_at = datetime(2025, 6, 30, 23, 59, tzinfo=UTC)
    holding.quantity = Decimal(quantity)
    holding.cost_basis = Decimal(cost_basis)
    holding.cost_basis_currency = currency
    holding.market_value = Decimal(market_value)
    holding.currency_code = currency
    holding.price = Decimal(price)
    holding.price_currency = currency
    holding.source = "provider_sync"
    return holding


# ═══════════════════════════════════════════════════════════════════════
# Pre-built fixture instances
# ═══════════════════════════════════════════════════════════════════════

SECURITY_AAPL = make_security(
    ticker="AAPL",
    isin="US0378331005",
    name="Apple Inc.",
    security_type="stock",
)

SECURITY_MSFT = make_security(
    ticker="MSFT",
    isin="US5949181045",
    name="Microsoft Corp.",
    security_type="stock",
)

SECURITY_VWCE = make_security(
    ticker="VWCE",
    isin="IE00BK5BQT80",
    name="Vanguard FTSE All-World UCITS ETF",
    security_type="etf",
)

SECURITY_BTC = make_security(
    ticker="BTC",
    isin="",
    name="Bitcoin",
    security_type="crypto",
)

WF_ACCOUNT_BROKERAGE = make_wf_account(
    name="Brokerage Account",
    account_type="brokerage",
    provider_key="trading212",
    currency_code="EUR",
)

WF_ACCOUNT_CASH = make_wf_account(
    name="Cash Account",
    account_type="checking",
    provider_key="bunq",
    currency_code="EUR",
)

WF_TRANSACTION_BUY_AAPL = make_wf_transaction(
    txn_type="purchase",
    amount="-1505.00",
    currency="USD",
    description="Buy 10 AAPL",
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_AAPL.id,
)

WF_TRANSACTION_SELL_MSFT = make_wf_transaction(
    txn_type="sale",
    amount="2500.00",
    currency="USD",
    description="Sell 5 MSFT",
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_MSFT.id,
)

WF_TRANSACTION_BUY_VWCE = make_wf_transaction(
    txn_type="purchase",
    amount="-2000.00",
    currency="EUR",
    description="Buy VWCE ETF",
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_VWCE.id,
)

WF_TRANSACTION_DEPOSIT = make_wf_transaction(
    txn_type="deposit",
    amount="5000.00",
    currency="EUR",
    description="Bank transfer",
    account_id=WF_ACCOUNT_CASH.id,
)

WF_TRANSACTION_WITHDRAWAL = make_wf_transaction(
    txn_type="withdrawal",
    amount="-500.00",
    currency="EUR",
    description="ATM withdrawal",
    account_id=WF_ACCOUNT_CASH.id,
)

WF_TRANSACTION_DIVIDEND = make_wf_transaction(
    txn_type="dividend",
    amount="50.00",
    currency="USD",
    description="AAPL Dividend",
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_AAPL.id,
)

WF_TRANSACTION_INTEREST = make_wf_transaction(
    txn_type="interest",
    amount="3.42",
    currency="EUR",
    description="Interest payment",
    account_id=WF_ACCOUNT_CASH.id,
)

WF_TRANSACTION_FEE = make_wf_transaction(
    txn_type="fee",
    amount="-9.99",
    currency="EUR",
    description="Brokerage fee",
    account_id=WF_ACCOUNT_BROKERAGE.id,
)

WF_TRANSACTION_TRANSFER_IN = make_wf_transaction(
    txn_type="transfer",
    amount="500.00",
    currency="EUR",
    description="Transfer in",
    account_id=WF_ACCOUNT_BROKERAGE.id,
)

WF_TRANSACTION_TRANSFER_OUT = make_wf_transaction(
    txn_type="transfer",
    amount="-200.00",
    currency="EUR",
    description="Transfer out",
    account_id=WF_ACCOUNT_BROKERAGE.id,
)

WF_HOLDING_AAPL = make_wf_holding(
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_AAPL.id,
    quantity="50",
    cost_basis="8574.00",
    market_value="9500.00",
    price="190.00",
    currency="USD",
)

WF_HOLDING_VWCE = make_wf_holding(
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_VWCE.id,
    quantity="20",
    cost_basis="4000.00",
    market_value="4200.00",
    price="210.00",
    currency="EUR",
)

WF_HOLDING_BTC = make_wf_holding(
    account_id=WF_ACCOUNT_BROKERAGE.id,
    security_id=SECURITY_BTC.id,
    quantity="0.5",
    cost_basis="15000.00",
    market_value="16000.00",
    price="32000.00",
    currency="USD",
)

WF_ACCOUNT_DEPOSIT_ONLY = make_wf_account(
    name="Deposit Account",
    account_type="savings",
    provider_key="bunq",
    currency_code="EUR",
)

WF_TRANSACTION_DEPOSIT_ONLY = make_wf_transaction(
    txn_type="deposit",
    amount="1000.00",
    currency="EUR",
    description="Initial deposit",
    account_id=WF_ACCOUNT_DEPOSIT_ONLY.id,
)

# ═══════════════════════════════════════════════════════════════════════
# Map of transaction types → expected WF activity type
# ═══════════════════════════════════════════════════════════════════════

EXPECTED_TXN_TYPE_MAP: dict[str, str] = {
    "purchase": "BUY",
    "sale": "SELL",
    "deposit": "DEPOSIT",
    "withdrawal": "WITHDRAWAL",
    "dividend": "DIVIDEND",
    "interest": "INTEREST",
    "fee": "FEE",
    "transfer": "TRANSFER_IN",  # positive amount
}

WF_MAP_TEST_CASES: list[dict[str, Any]] = [
    {
        "txn": WF_TRANSACTION_BUY_AAPL,
        "security": SECURITY_AAPL,
        "expected_activity": "BUY",
        "expected_symbol": "AAPL",
        "expected_instrument": "EQUITY",
    },
    {
        "txn": WF_TRANSACTION_SELL_MSFT,
        "security": SECURITY_MSFT,
        "expected_activity": "SELL",
        "expected_symbol": "MSFT",
        "expected_instrument": "EQUITY",
    },
    {
        "txn": WF_TRANSACTION_DEPOSIT,
        "security": None,
        "expected_activity": "DEPOSIT",
        "expected_symbol": "",
        "expected_instrument": "",
    },
    {
        "txn": WF_TRANSACTION_DIVIDEND,
        "security": SECURITY_AAPL,
        "expected_activity": "DIVIDEND",
        "expected_symbol": "AAPL",
        "expected_instrument": "EQUITY",
    },
]
