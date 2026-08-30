"""Connector-owned Wealthfolio extension coverage and projection contract.

Wealthfolio does not expose stable import endpoints for every planning
feature.  This module creates a conservative JSON sidecar contract: datasets
that finance-sync can prove are available are projected, while unsupported
domains are reported explicitly instead of being silently invented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from finance_sync.services.spending_privacy import redact_destination_metadata

DATASETS = (
    "portfolios",
    "allocations",
    "goals",
    "spending",
    "net_worth",
    "alternative_assets",
    "notes",
    "tags",
    "events",
)


def build_extension_payload(
    *,
    accounts: list[Any],
    metadata: list[Any] | None = None,
    transactions: list[Any] | None = None,
) -> dict[str, Any]:
    """Build a lossless, connector-owned extension sidecar.

    Provider metadata may contain future extension datasets.  Only explicitly
    shaped dictionaries are copied; manual Wealthfolio state is never read or
    overwritten by this projection.
    """
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in DATASETS}
    for account in accounts:
        raw_provider_metadata = getattr(account, "provider_metadata", None)
        provider_metadata = (
            cast("dict[str, Any]", raw_provider_metadata)
            if isinstance(raw_provider_metadata, dict)
            else {}
        )
        sections["portfolios"].append(
            {
                "sourceRecordId": str(account.id),
                "name": account.name,
                "accountIds": [str(account.id)],
                "currency": account.currency_code,
                "sourceSystem": "FINANCE_SYNC",
            }
        )
        for dataset in DATASETS:
            values: Any = provider_metadata.get(dataset)
            if isinstance(values, dict):
                values = [values]
            if isinstance(values, list):
                for value in cast(list[Any], values):
                    if not isinstance(value, dict):
                        continue
                    record = cast("dict[str, Any]", value)
                    record = cast(
                        "dict[str, Any]",
                        redact_destination_metadata(record),
                    )
                    record.setdefault("accountId", str(account.id))
                    record.setdefault("sourceSystem", "FINANCE_SYNC")
                    sections[dataset].append(record)

    if metadata:
        for observation in metadata:
            if observation.metadata_type in {
                "sector_exposure",
                "etf_composition",
            }:
                sections["allocations"].append(
                    {
                        "sourceRecordId": str(observation.id),
                        "securityId": str(observation.security_id),
                        "asOf": (
                            observation.timestamp.astimezone(UTC).isoformat()
                        ),
                        "type": observation.metadata_type,
                        "label": observation.label,
                        "source": observation.source,
                        "data": observation.metadata_json,
                    }
                )

    for transaction in transactions or []:
        suggestion = getattr(transaction, "cashflow_suggestion", None)
        if suggestion is not None and hasattr(suggestion, "model_dump"):
            suggestion = suggestion.model_dump(mode="json")
        split_values: list[Any] = list(
            getattr(transaction, "splits", None) or []
        )
        sections["spending"].append(
            {
                "sourceRecordId": str(transaction.id),
                "externalTransactionId": str(
                    transaction.external_transaction_id
                ),
                "accountId": str(transaction.account_id),
                "merchant": getattr(transaction, "merchant_name", None),
                "merchantId": getattr(transaction, "merchant_id", None),
                "mcc": getattr(transaction, "merchant_category_code", None),
                "cashflowBucket": getattr(transaction, "cashflow_bucket", None),
                "categoryAssignment": suggestion,
                "notes": getattr(transaction, "description", None),
                "splits": [
                    _split_payload(split)
                    for split in split_values
                ],
                "sourceSystem": "FINANCE_SYNC",
            }
        )
        for event in list(getattr(transaction, "lifecycle_events", None) or []):
            occurred_at = getattr(event, "created_at", None)
            sections["events"].append(
                {
                    "sourceRecordId": str(event.id),
                    "transactionId": str(transaction.id),
                    "eventType": getattr(event, "event_type", None),
                    "occurredAt": (
                        occurred_at.astimezone(UTC).isoformat()
                        if occurred_at is not None
                        else None
                    ),
                    "actor": getattr(event, "actor", None),
                    "payload": getattr(event, "payload", None),
                    "provenance": getattr(event, "provenance", None),
                    "sourceSystem": "FINANCE_SYNC",
                }
            )

    coverage = {
        dataset: {
            "status": "available" if sections[dataset] else "unavailable",
            "records": len(sections[dataset]),
        }
        for dataset in DATASETS
    }
    return {
        "schemaVersion": "1.0",
        "generatedAt": datetime.now(UTC).isoformat(),
        "sourceSystem": "FINANCE_SYNC",
        "coverage": coverage,
        "datasets": sections,
    }


def _split_payload(split: Any) -> dict[str, Any]:
    """Project an optional split without requiring a loaded ORM type."""
    return {
        "amount": str(getattr(split, "amount", "0")),
        "currency": getattr(split, "currency_code", None),
        "destination": getattr(split, "destination", None),
        "provenance": getattr(split, "provenance", None),
    }
