"""Map canonical finance-sync transactions to Ghostfolio activities."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.models.holding import Holding
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
    # Broker tickers commonly contain an exchange suffix (for example
    # ``BESI:XAMS``), while Ghostfolio's Yahoo data source expects the
    # provider ticker itself (``BESI``).  Keep ISINs and manual symbols intact.
    if (
        symbol
        and security
        and security.ticker
        and ":" in security.ticker
        and data_source.upper() == "YAHOO"
    ):
        symbol = security.ticker.split(":", 1)[0]
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


def map_holding_to_ghostfolio(
    holding: Holding,
    *,
    security: Security | None = None,
    data_source: str = "MANUAL",
    ghostfolio_account_id: str | None = None,
) -> dict[str, Any]:
    """Map a current finance-sync position to a Ghostfolio BUY activity.

    Ghostfolio represents a current position as the net result of activities.
    A snapshot is therefore imported as a dated BUY using the observed
    quantity and market price.  Manual symbols preserve broker exchange
    suffixes (for example ``BESI:XAMS``), which are not Yahoo symbols.
    """
    symbol = (security.ticker or security.isin) if security else None
    if not symbol:
        message = "Ghostfolio holdings require a security symbol"
        raise ValueError(message)
    quantity = abs(Decimal(holding.quantity))
    if quantity == 0:
        message = "Ghostfolio holdings require a non-zero quantity"
        raise ValueError(message)
    # Market value is already converted to the holding currency by the
    # source connector; prefer it over the broker's native unit price.
    unit_price = None
    if holding.market_value is not None:
        unit_price = Decimal(holding.market_value) / quantity
    elif holding.price is not None:
        unit_price = holding.price
    if unit_price is None:
        message = "Ghostfolio holdings require a market price"
        raise ValueError(message)
    activity = {
        "currency": holding.currency_code,
        "dataSource": data_source,
        "date": holding.observed_at.isoformat(),
        "fee": 0.0,
        "quantity": float(quantity),
        "symbol": str(symbol),
        "type": "BUY",
        "unitPrice": float(abs(Decimal(unit_price))),
        "comment": f"finance-sync:holding:{holding.id}",
    }
    if ghostfolio_account_id:
        activity["accountId"] = ghostfolio_account_id
    return activity
