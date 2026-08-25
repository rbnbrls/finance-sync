"""Map canonical investment transactions to InvestBrain."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.models.security import Security
    from finance_sync.models.transaction import Transaction


def map_transaction_to_investbrain(
    txn: Transaction, *, portfolio_id: str, security: Security | None = None
) -> dict[str, Any] | None:
    transaction_type = str(txn.transaction_type).lower()
    if transaction_type not in {"purchase", "sale"}:
        return None
    symbol = (security.ticker or security.isin) if security else None
    if not symbol:
        return None
    quantity = abs(Decimal(txn.quantity or 0))
    if quantity <= 0:
        return None
    unit_price = abs(Decimal(txn.unit_price or 0))
    if unit_price <= 0:
        unit_price = abs(Decimal(txn.amount)) / quantity
    return {
        "symbol": str(symbol).upper(),
        "portfolio_id": portfolio_id,
        "transaction_type": "BUY" if transaction_type == "purchase" else "SELL",
        "quantity": float(quantity),
        "currency": txn.currency_code,
        "date": txn.occurred_at.date().isoformat(),
        "cost_basis": float(unit_price)
        if transaction_type == "purchase"
        else 0,
        "sale_price": float(unit_price) if transaction_type == "sale" else None,
        "split": False,
        "reinvested_dividend": False,
    }
