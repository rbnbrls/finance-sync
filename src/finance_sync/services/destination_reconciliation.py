"""Pure reconciliation helpers for destination exports."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def reconcile_destination(
    canonical: list[Any],
    destination: list[dict[str, Any]],
    *,
    destination_type: str,
) -> list[dict[str, Any]]:
    """Find missing, duplicate and semantic destination mismatches.

    The destination adapter supplies native records; this function never
    mutates either side and therefore is safe to use in dry-run previews.
    """
    findings: list[dict[str, Any]] = []
    expected: dict[str, Any] = {
        f"{item.provider_key}:{item.external_transaction_id}": item
        for item in canonical
    }
    actual: dict[str, list[dict[str, Any]]] = {}
    for item in destination:
        key = str(
            item.get("import_id")
            or item.get("external_id")
            or item.get("id", "")
        )
        actual.setdefault(key, []).append(item)
        if key.startswith("finance-sync:"):
            actual.setdefault(key.removeprefix("finance-sync:"), []).append(
                item
            )
    for key, item in expected.items():
        native_key = f"{item.provider_key}:{item.external_transaction_id}"
        matches = actual.get(native_key) or actual.get(
            str(item.external_transaction_id), []
        )
        if not matches:
            findings.append(
                {
                    "kind": "missing_transaction",
                    "key": key,
                    "destination": destination_type,
                }
            )
            continue
        if len(matches) > 1:
            findings.append(
                {
                    "kind": "duplicate_transaction",
                    "key": key,
                    "count": len(matches),
                    "destination": destination_type,
                }
            )
        first = matches[0]
        destination_amount = first.get("amount")
        if destination_amount is not None and destination_type == "ynab":
            destination_amount = Decimal(str(destination_amount)) / 1000
        if destination_amount is not None and Decimal(
            str(destination_amount)
        ) != Decimal(str(item.amount)):
            findings.append(
                {
                    "kind": "amount_mismatch",
                    "key": key,
                    "expected": str(item.amount),
                    "actual": str(destination_amount),
                }
            )
        if (
            "currency_code" in first
            and str(first["currency_code"]).upper()
            != str(item.currency_code).upper()
        ):
            findings.append({"kind": "currency_mismatch", "key": key})
        if (
            any(
                key in first
                for key in ("category", "category_name", "category_id")
            )
            and getattr(item, "cashflow_bucket", None)
            and next(
                (
                    first[key]
                    for key in ("category", "category_name", "category_id")
                    if key in first
                ),
                None,
            )
            != item.cashflow_bucket
        ):
            findings.append({"kind": "category_mismatch", "key": key})
    for key in actual:
        normalized_key = key.removeprefix("finance-sync:")
        if key and not any(
            normalized_key in {expected_key, str(item.external_transaction_id)}
            for expected_key, item in expected.items()
        ):
            findings.append(
                {
                    "kind": "unmatched_destination_object",
                    "key": key,
                    "destination": destination_type,
                }
            )
    return findings
