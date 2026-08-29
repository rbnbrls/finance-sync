"""Connector-owned Wealthfolio extension coverage and projection contract.

Wealthfolio does not expose stable import endpoints for every planning
feature.  This module creates a conservative JSON sidecar contract: datasets
that finance-sync can prove are available are projected, while unsupported
domains are reported explicitly instead of being silently invented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

DATASETS = (
    "portfolios",
    "allocations",
    "goals",
    "spending",
    "net_worth",
    "alternative_assets",
    "notes",
    "tags",
)


def build_extension_payload(
    *,
    accounts: list[Any],
    metadata: list[Any] | None = None,
) -> dict[str, Any]:
    """Build a lossless, connector-owned extension sidecar.

    Provider metadata may contain future extension datasets.  Only explicitly
    shaped dictionaries are copied; manual Wealthfolio state is never read or
    overwritten by this projection.
    """
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in DATASETS}
    for account in accounts:
        provider_metadata = getattr(account, "provider_metadata", None)
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}
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
            values = provider_metadata.get(dataset)
            if isinstance(values, dict):
                values = [values]
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    record = dict(value)
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
