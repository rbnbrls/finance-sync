"""Shared rules for deciding whether duplicate candidates are meaningful."""

from __future__ import annotations


def has_distinct_transaction_ids_in_descriptions(
    transaction_a: object, transaction_b: object
) -> bool:
    """Return whether both descriptions identify their own different IDs.

    Providers such as Trading212 can emit several legitimate transactions
    with the same amount.  When each description contains that transaction's
    own provider ID, the pair is demonstrably not a duplicate even if the
    amount and booking date happen to match.
    """
    external_a = _normalise_id(
        getattr(transaction_a, "external_transaction_id", None)
    )
    external_b = _normalise_id(
        getattr(transaction_b, "external_transaction_id", None)
    )
    description_a = _normalise_text(getattr(transaction_a, "description", None))
    description_b = _normalise_text(getattr(transaction_b, "description", None))

    if not external_a or not external_b or external_a == external_b:
        return False
    return _contains_id(description_a, external_a) and _contains_id(
        description_b, external_b
    )


def _normalise_text(value: object) -> str:
    return str(value or "").strip().lower()


def _normalise_id(value: object) -> str:
    value = _normalise_text(value)
    # The UI/source data may add the canonical transaction prefix while the
    # provider puts the bare ID in the description.
    return value.removeprefix("txn_")


def _contains_id(description: str, transaction_id: str) -> bool:
    return bool(
        description and transaction_id and transaction_id in description
    )
