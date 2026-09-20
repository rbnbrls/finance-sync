"""Canonical spending categories shared by downstream destinations.

The labels deliberately follow the user's Wealthfolio taxonomy.  Provider
connectors may still emit legacy values (``health``/``utilities``/``other``)
for old rows; :func:`canonicalize_category` keeps those rows compatible while
new imports use the complete taxonomy.
"""

from __future__ import annotations

TRANSACTION_CATEGORY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("employment", "Employment"),
    ("housing", "Housing"),
    ("groceries", "Groceries"),
    ("food_and_dining", "Food & Dining"),
    ("transportation", "Transportation"),
    ("shopping", "Shopping"),
    ("entertainment", "Entertainment"),
    ("health_wellness", "Health & Wellness"),
    ("bills_and_utilities", "Bills & Utilities"),
    ("personal_care", "Personal Care"),
    ("education", "Education"),
    ("travel", "Travel"),
    ("gifts_and_donations", "Gifts & Donations"),
    ("fees_and_charges", "Fees & Charges"),
    ("finance", "Finance"),
    ("other_expenses", "Other Expenses"),
)

TRANSACTION_CATEGORY_VALUES = frozenset(
    value for value, _label in TRANSACTION_CATEGORY_OPTIONS
)

_CATEGORY_ALIASES = {
    "health": "health_wellness",
    "software": "bills_and_utilities",
    "utilities": "bills_and_utilities",
    "other": "other_expenses",
    "other_expense": "other_expenses",
    "health_and_wellness": "health_wellness",
    "bills_utilities": "bills_and_utilities",
    "fees": "fees_and_charges",
    "gift_and_donations": "gifts_and_donations",
}


def canonicalize_category(value: str | None) -> str | None:
    """Return a canonical category key for provider and legacy labels."""
    if not value:
        return None
    normalized = "_".join(
        str(value).strip().casefold().replace("&", "and").split()
    )
    if normalized in TRANSACTION_CATEGORY_VALUES:
        return normalized
    return _CATEGORY_ALIASES.get(normalized)
