"""Canonical → Actual Budget transaction mapping.

Translates a finance-sync ``Transaction`` ORM row into the dict
that actualpy's ``create_transaction`` expects.

Mapping rules
-------------
* Amount signs:  finance-sync uses positive = inflow, negative = outflow.
  actualpy / Actual Budget uses the same convention, so the sign is kept
  as-is but converted to cents (integer) via ``decimal_to_cents``.
* ``imported_id`` is derived from the canonical
  ``external_transaction_id`` so that Actual Budget's dedup logic
  can recognise re-exports of the same transaction.
* Transaction types are mapped to the nearest AB-compatible description
  for payee naming.
* Currency conversion fields (``amount_in_base``, ``fx_rate``) are
  preserved in the transaction notes.
* Investment transactions (buys, sells, dividends, fees) are flagged
  in the notes with a prefix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.models.transaction import Transaction as FsTransaction

# ── Public API ──────────────────────────────────────────────────────────


def map_transaction(
    txn: FsTransaction,
    *,
    ab_account_name: str,
    fallback_payee: str = "Imported transaction",
) -> dict[str, Any]:
    """Convert a canonical *txn* into an actualpy-compatible dict.

    The returned dict can be passed to ``create_transaction`` (or
    ``create_transaction_from_ids``) after resolving the payee / category
    ids.

    Args:
        txn:              finance-sync Transaction ORM row.
        ab_account_name:  Target Actual Budget account name (must exist).
        fallback_payee:   Default payee description when the transaction
                          has no description.

    Returns:
        A dict with keys ``date``, ``account``, ``payee``, ``notes``,
        ``amount``, ``imported_id``, ``cleared``, ``imported_payee``.
    """
    occurred: date = _as_date(txn.occurred_at)

    payee = _build_payee(txn, fallback_payee)
    notes = _build_notes(txn)
    imported_id = _build_imported_id(txn)
    amount = _cents(txn.amount)

    return {
        "date": occurred,
        "account": ab_account_name,
        "payee": payee,
        "notes": notes,
        "amount": amount,
        "imported_id": imported_id,
        "cleared": txn.status == "booked",
        "imported_payee": _build_imported_payee(txn),
    }


def map_transaction_to_csv_row(txn: FsTransaction) -> dict[str, str]:
    """Map a canonical transaction to a CSV row compatible with AB import.

    Returns a dict with keys ``Date``, ``Payee``, ``Category``,
    ``Notes``, ``Amount``.
    """
    occurred: date = _as_date(txn.occurred_at)
    description = txn.description or ""
    notes = _build_notes(txn)

    return {
        "Date": occurred.isoformat(),
        "Payee": description,
        "Category": "",
        "Notes": notes,
        "Amount": str(Decimal(txn.amount).quantize(Decimal("0.01"))),
    }


# ── Internal helpers ────────────────────────────────────────────────────


def _build_payee(
    txn: FsTransaction,
    fallback: str,
) -> str:
    """Derive a payee name from the transaction."""
    if txn.description:
        return txn.description

    # Map transaction type to a sensible payee name
    type_labels: dict[str, str] = {
        "purchase": "Purchase",
        "sale": "Sale",
        "payment": "Payment",
        "withdrawal": "Withdrawal",
        "deposit": "Deposit",
        "fee": "Bank Fee",
        "interest": "Interest Payment",
        "dividend": "Dividend",
        "transfer": "Transfer",
    }
    return type_labels.get(txn.transaction_type, fallback)


def _build_notes(txn: FsTransaction) -> str | None:
    """Assemble human-readable notes for the transaction.

    Includes FX information and the provider reference when available.
    """
    parts: list[str] = []

    if txn.description:
        parts.append(txn.description)

    # FX conversion info
    if txn.amount_in_base is not None and txn.base_currency_code:
        fx_info = (
            f"FX: {txn.amount} {txn.currency_code} → "
            f"{txn.amount_in_base} {txn.base_currency_code}"
        )
        if txn.fx_rate is not None:
            fx_info += f" @ {txn.fx_rate}"
        parts.append(fx_info)

    # Transaction type hint for investment transactions
    if txn.transaction_type in ("sale", "dividend", "fee"):
        if txn.security_id:
            parts.append(f"Type: {txn.transaction_type}")

    # Provider fingerprint
    if txn.provider_fingerprint:
        parts.append(f"Ref: {txn.provider_fingerprint}")

    return "\n".join(parts) if parts else None


def _build_imported_id(txn: FsTransaction) -> str:
    """Deterministic external ID that AB can use for dedup.

    Format: ``fs_{external_transaction_id}``
    """
    return f"fs_{txn.external_transaction_id}"


def _build_imported_payee(txn: FsTransaction) -> str | None:
    """Return the raw provider payee / description for AB's imported_payee.

    This value is shown in the AB UI as the original import description
    and is used for rule matching.
    """
    return txn.description


def _cents(amount: Decimal) -> int:
    """Convert a Decimal amount to integer cents (Actual's internal format).

    Actual Budget stores amounts as integers representing the
    value * 100 (for most currencies).  This conversion handles that.
    """
    cents = amount * 100
    return int(cents.quantize(Decimal("1")))


def _as_date(dt: datetime) -> date:
    """Convert a timezone-aware datetime to a date."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).date()
    return dt.date()
