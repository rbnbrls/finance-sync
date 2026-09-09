"""Shared connector capability contract for spending integrations."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field


class CapabilityAvailability(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCREMENTAL = "incremental"
    HISTORICAL = "historical"
    DETAIL_ONLY = "detail_only"


class ConnectorCapability(BaseModel):
    """Describes how a connector exposes an optional resource."""

    name: str
    availability: CapabilityAvailability
    privacy_redacted: bool = False
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


SPENDING_CAPABILITIES = (
    "merchant_data",
    "mcc_category",
    "card_transactions",
    "refunds_chargebacks",
    "notes",
    "attachments",
    "scheduled_payments",
    "recurring_patterns",
    "transfer_links",
)


def normalize_capabilities(raw: Any) -> dict[str, ConnectorCapability]:
    """Validate class-level capability declarations safely."""
    if not isinstance(raw, dict):
        return {}
    raw_dict = cast("dict[str, Any]", raw)
    result: dict[str, ConnectorCapability] = {}
    for name, value in raw_dict.items():
        if not name:
            continue
        try:
            details: dict[str, Any] = (
                dict(cast("dict[str, Any]", value))
                if isinstance(value, dict)
                else {}
            )
            availability = details.pop("availability", value)
            result[name] = ConnectorCapability(
                name=name,
                availability=availability,
                **details,
            )
        except (TypeError, ValueError):
            continue
    return result
