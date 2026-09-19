"""Destination capability contracts and native transaction projections."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class DestinationCapability:
    name: str
    mode: str  # read, write, read_write, or unsupported
    bidirectional: bool = False


_MATRIX: dict[str, tuple[DestinationCapability, ...]] = {
    "wealthfolio": (
        DestinationCapability("accounts", "write"),
        DestinationCapability("cash_activities", "write"),
        DestinationCapability("category_assignments", "write"),
        DestinationCapability("splits", "write"),
        DestinationCapability("events", "write"),
        DestinationCapability("notes", "write"),
        DestinationCapability("attachments", "read"),
    ),
    "actual-budget": (
        DestinationCapability("accounts", "write"),
        DestinationCapability("transactions", "write"),
        DestinationCapability("categories", "read_write"),
        DestinationCapability("transfers", "write"),
        DestinationCapability("splits", "write"),
        DestinationCapability("notes", "write"),
        DestinationCapability("budgets", "read_write"),
    ),
    "ynab": (
        DestinationCapability("accounts", "read_write"),
        DestinationCapability("transactions", "write"),
        DestinationCapability("categories", "read"),
        DestinationCapability("transfers", "write"),
        DestinationCapability("cleared_pending", "write"),
        DestinationCapability("import_ids", "write"),
    ),
    "firefly": (
        DestinationCapability("accounts", "read_write"),
        DestinationCapability("transactions", "write"),
        DestinationCapability("categories", "read_write"),
        DestinationCapability("tags", "write"),
        DestinationCapability("bills", "read_write"),
        DestinationCapability("budgets", "read_write"),
        DestinationCapability("splits", "write"),
    ),
}


def destination_capabilities(
    destination: str,
) -> dict[str, DestinationCapability]:
    """Return a copy so callers cannot mutate the contract globally."""
    return {item.name: item for item in _MATRIX.get(destination, ())}


def destination_is_bidirectional(destination: str) -> bool:
    return any(item.bidirectional for item in _MATRIX.get(destination, ()))


def native_transaction_projection(
    destination: str,
    transaction: Any,
    *,
    account_name: str | None = None,
) -> dict[str, Any]:
    """Project shared semantics without imposing destination categories.

    This contract is intentionally a projection, not a universal CSV format:
    adapters remain responsible for resolving native IDs and API semantics.
    """
    amount = Decimal(str(transaction.amount))
    payload: dict[str, Any] = {
        "canonical_id": str(transaction.id),
        "external_id": str(transaction.external_transaction_id),
        "date": transaction.occurred_at.isoformat(),
        "amount": amount,
        "currency_code": str(transaction.currency_code).upper(),
        "status": str(transaction.status),
        "account": account_name,
        "payee": getattr(transaction, "merchant_name", None)
        or getattr(transaction, "counterparty_name", None)
        or transaction.description,
        "category_suggestion": getattr(
            transaction, "cashflow_suggestion", None
        ),
        "splits": getattr(transaction, "splits", None),
        "notes": getattr(transaction, "description", None),
    }
    if destination == "firefly":
        payload["type"] = "deposit" if amount >= 0 else "withdrawal"
        payload["source_name"] = (
            account_name if amount < 0 else "finance-sync income"
        )
        payload["destination_name"] = (
            account_name if amount >= 0 else payload["payee"]
        )
    elif destination == "ynab":
        payload["import_id"] = (
            f"finance-sync:{transaction.provider_key}:{transaction.external_transaction_id}"
        )
        payload["cleared"] = (
            "cleared" if str(transaction.status) == "booked" else "uncleared"
        )
    elif destination == "actual-budget":
        payload["imported_id"] = (
            f"finance-sync:{transaction.provider_key}:{transaction.external_transaction_id}"
        )
    return payload


def build_destination_preview(
    destination: str,
    transactions: list[Any],
    accounts: list[Any],
) -> dict[str, Any]:
    """Return a side-effect-free preview for a new destination."""
    capabilities = destination_capabilities(destination)
    unmapped: set[str] = set()
    for transaction in transactions:
        if (
            getattr(transaction, "splits", None)
            and "splits" not in capabilities
        ):
            unmapped.add("splits")
        if (
            getattr(transaction, "cashflow_suggestion", None)
            and "category_assignments" not in capabilities
            and "categories" not in capabilities
        ):
            unmapped.add("category_assignments")
        if (
            getattr(transaction, "annotations", None)
            and "notes" not in capabilities
        ):
            unmapped.add("notes")
    return {
        "destination": destination,
        "transaction_count": len(transactions),
        "account_count": len(accounts),
        "would_create": {
            "accounts": len(accounts),
            "transactions": len(transactions),
        },
        "unmapped_capabilities": sorted(unmapped),
        "bidirectional": destination_is_bidirectional(destination),
        "dry_run": True,
    }
