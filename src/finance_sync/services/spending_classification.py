"""Pure merchant normalization, category suggestions and safe merges."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from finance_sync.connectors.models import (
    CanonicalTransactionData,
    CategorySuggestion,
)


@dataclass(frozen=True)
class MerchantMapping:
    merchant_key: str
    display_name: str
    category: str | None = None
    taxonomy: str | None = None
    destination_type: str | None = None
    destination_category: str | None = None
    normalization_version: str = "1"


def normalize_merchant_key(
    name: str | None, country: str | None = None
) -> str | None:
    """Build a stable, provider-independent merchant key."""
    if not name or not name.strip():
        return None
    value = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return ":".join(
        part for part in (value, (country or "").casefold()) if part
    )


def suggest_category(
    transaction: CanonicalTransactionData,
    mappings: dict[str, MerchantMapping] | None = None,
) -> CategorySuggestion | None:
    """Select a suggestion while retaining its source and confidence."""
    if transaction.classification_override:
        return CategorySuggestion(
            value=transaction.classification_override,
            source="user_override",
            confidence=1,
        )
    key = normalize_merchant_key(
        transaction.merchant_name, transaction.merchant_country
    )
    if mappings and key in mappings and mappings[key].category:
        mapping = mappings[key]
        return CategorySuggestion(
            value=mapping.category or "",
            source="merchant_mapping",
            confidence=1,
            taxonomy=mapping.taxonomy,
        )
    if transaction.merchant_category_code:
        return CategorySuggestion(
            value=transaction.merchant_category_code,
            source="mcc",
            confidence=0.5,
        )
    return transaction.cashflow_suggestion


def merge_destination_enrichment(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge source refreshes without replacing destination/user fields."""
    protected = {
        "category",
        "category_assignment",
        "splits",
        "events",
        "notes",
        "destination_override",
    }
    result = dict(existing)
    result.update(
        {
            key: value
            for key, value in incoming.items()
            if value is not None and key not in protected
        }
    )
    result.update(overrides or {})
    return result
