"""Map canonical finance-sync transactions to Firefly transaction splits."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast


def map_transaction(
    transaction: Any,
    *,
    account_name: str,
    import_tag: str = "finance-sync",
    budget_name: str | None = None,
    bill_id: str | None = None,
) -> dict[str, Any]:
    """Return a valid Firefly split for a canonical transaction.

    The canonical account is always the asset side. Positive amounts become
    deposits from a revenue account; negative amounts become withdrawals to
    an expense account. Transfers are represented in the same direction-safe
    way when the canonical transaction has no counterpart account.
    """
    amount = Decimal(str(transaction.amount))
    positive = amount >= 0
    tx_type = "deposit" if positive else "withdrawal"
    description = str(
        getattr(transaction, "merchant_name", None)
        or transaction.description
        or transaction.transaction_type
    )
    payload: dict[str, Any] = {
        "type": tx_type,
        "date": transaction.occurred_at.isoformat(),
        "amount": str(abs(amount).quantize(Decimal("0.01"))),
        "description": description[:1024],
        "currency_code": str(transaction.currency_code or "EUR").upper(),
        "external_id": str(transaction.external_transaction_id),
        "notes": f"finance-sync:{transaction.id}",
        "tags": [import_tag],
        "reconciled": str(transaction.status) == "booked",
    }
    if positive:
        payload["destination_name"] = account_name
        payload["source_name"] = "finance-sync income"
    else:
        payload["source_name"] = account_name
        payload["destination_name"] = (
            description[:256] or "finance-sync expense"
        )
    if getattr(transaction, "merchant_category_code", None):
        payload["category_name"] = str(transaction.merchant_category_code)
    if getattr(transaction, "cashflow_suggestion", None):
        suggestion: Any = transaction.cashflow_suggestion
        if isinstance(suggestion, dict):
            mapping = cast(dict[str, Any], suggestion)
            suggestion = mapping.get("value") or mapping.get("category")
        else:
            suggestion = getattr(suggestion, "value", suggestion)
        if suggestion:
            payload["category_name"] = str(suggestion)
    if budget_name:
        payload["budget_name"] = budget_name
    if bill_id:
        payload["bill_id"] = bill_id
    if getattr(transaction, "splits", None):
        payload["canonical_splits"] = [
            {
                "amount": str(getattr(split, "amount", "0")),
                "currency_code": getattr(
                    split, "currency_code", transaction.currency_code
                ),
                "percentage": getattr(split, "percentage", None),
                "category": getattr(
                    getattr(split, "category_suggestion", None),
                    "value",
                    getattr(split, "category_suggestion", None),
                ),
                "destination": getattr(split, "destination", None),
                "provenance": getattr(split, "provenance", None),
            }
            for split in transaction.splits
        ]
    return payload
