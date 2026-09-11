"""Map canonical transactions to YNAB's native transaction semantics."""
# ruff: noqa: E501

from __future__ import annotations

from decimal import Decimal
from typing import Any


def map_transaction(
    transaction: Any,
    *,
    account_id: str,
    category_id: str | None = None,
    transfer_account_id: str | None = None,
) -> dict[str, Any]:
    """Build a YNAB API transaction without leaking other taxonomies."""
    amount = int((Decimal(str(transaction.amount)) * 1000).quantize(1))
    payload: dict[str, Any] = {
        "account_id": account_id,
        "date": transaction.occurred_at.date().isoformat(),
        "amount": amount,
        "payee_name": getattr(transaction, "merchant_name", None)
        or transaction.description
        or transaction.transaction_type,
        "memo": transaction.description,
        "cleared": "cleared"
        if str(transaction.status) == "booked"
        else "uncleared",
        "approved": False,
        "import_id": f"finance-sync:{transaction.provider_key}:{transaction.external_transaction_id}",
    }
    if category_id and not transfer_account_id:
        payload["category_id"] = category_id
    if transfer_account_id or transaction.transaction_type == "transfer":
        payload["transfer_account_id"] = transfer_account_id
        payload.pop("category_id", None)
    splits = getattr(transaction, "splits", None) or ()
    if splits:
        payload["subtransactions"] = [
            {
                "amount": int((Decimal(str(split.amount)) * 1000).quantize(1)),
                "category_id": getattr(split, "destination", None),
                "memo": getattr(split, "provenance", None),
            }
            for split in splits
        ]
    return payload
