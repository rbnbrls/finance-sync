"""Map canonical finance-sync transactions to Ghostfolio activities."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.models.security import Security
    from finance_sync.models.transaction import Transaction


TYPE_MAP = {
    "purchase": "BUY",
    "sale": "SELL",
    "dividend": "DIVIDEND",
    "fee": "FEE",
    "payment": "FEE",
    "interest": "INTEREST",
    "tax": "FEE",
}


def map_transaction_to_ghostfolio(
    txn: Transaction,
    *,
    security: Security | None = None,
    data_source: str = "YAHOO",
) -> dict[str, Any]:
    """Return the JSON shape accepted by ``POST /api/v1/import``."""
    txn_type = str(txn.transaction_type).lower()
    activity_type = TYPE_MAP.get(txn_type)
    if activity_type is None:
        message = f"Ghostfolio does not support transaction type {txn_type!r}"
        raise ValueError(message)
    symbol = (security.ticker or security.isin) if security else None
    if not symbol:
        symbol = (
            txn.description or f"FINANCE-SYNC-{txn.external_transaction_id}"
        )
        data_source = "MANUAL"
    quantity = abs(Decimal(txn.quantity or 1))
    if (
        txn_type in {"fee", "payment", "tax", "interest", "dividend"}
        and not txn.quantity
    ):
        quantity = Decimal(1)
    unit_price = abs(Decimal(txn.unit_price or 0))
    if unit_price == 0 and txn_type in {
        "fee",
        "payment",
        "tax",
        "interest",
        "dividend",
    }:
        unit_price = abs(Decimal(txn.amount))
    return {
        "currency": txn.currency_code,
        "dataSource": data_source,
        "date": txn.occurred_at.isoformat(),
        "fee": float(abs(Decimal(txn.fee_amount or 0))),
        "quantity": float(quantity),
        "symbol": str(symbol),
        "type": activity_type,
        "unitPrice": float(unit_price),
        "comment": f"finance-sync:{txn.id}:{txn.external_transaction_id}",
    }
