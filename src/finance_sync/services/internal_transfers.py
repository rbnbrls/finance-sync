"""Provider-aware detection of transfers between a user's own accounts."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

if TYPE_CHECKING:
    from collections.abc import Iterable

    from finance_sync.connectors.models import CanonicalTransactionData


_EASY_BUDGETING_PATTERNS = (
    (
        "top_up",
        re.compile(
            r"^(?:automatic top-up of your (.+?) budget|"
            r"automatic budget top up)\.?$",
            re.IGNORECASE,
        ),
    ),
    (
        "remainder",
        re.compile(r"^remainder of your (.+?) budget\.?$", re.IGNORECASE),
    ),
)


def is_bunq_easy_budgeting_transaction(transaction: object) -> bool:
    """Return whether *transaction* is a Bunq Easy Budgeting movement.

    These movements are internal bookkeeping between Bunq budget accounts.
    They must not be projected to Wealthfolio Spending at all; unlike a
    normal bank transfer, they are not user-visible cashflow.
    """
    if str(getattr(transaction, "provider_key", "")).lower() != "bunq":
        return False
    metadata = getattr(transaction, "provider_metadata", None) or {}
    detection = str(metadata.get("internal_transfer_detection") or "")
    if detection.startswith("bunq_easy_budgeting"):
        return True
    description = str(getattr(transaction, "description", "") or "").strip()
    return any(
        pattern.match(description) for _, pattern in _EASY_BUDGETING_PATTERNS
    )


async def exclude_unlabeled_bunq_transfer_pairs(
    session: Any,
    *,
    tenant_id: str,
    connection_id: str | None,
) -> int:
    """Exclude Bunq's unlabeled cross-account Easy Budgeting legs.

    Some Bunq budget movements arrive as two generic ``PAYMENT`` rows with
    no description, counterparty IBAN, or allocation subtype.  Salary
    distribution movements are similar, but carry the same ``Salaris ...``
    description on both legs.  The reliable evidence is the paired opposite
    amount between two Bunq accounts at the same instant.  Pair rows
    one-to-one within a small window so normal spending is not broadly
    classified as a transfer.
    """
    from finance_sync.models import Transaction

    stmt = select(Transaction).where(
        Transaction.tenant_id == tenant_id,
        Transaction.provider_key == "bunq",
        Transaction.status.in_(["booked", "pending"]),
        or_(
            Transaction.counterparty_account_reference.is_(None),
            Transaction.counterparty_account_reference == "",
        ),
        or_(
            Transaction.description.is_(None),
            Transaction.description == "",
            Transaction.description.ilike("salaris %"),
        ),
    )
    if connection_id is None:
        stmt = stmt.where(Transaction.connection_id.is_(None))
    else:
        stmt = stmt.where(Transaction.connection_id == connection_id)
    rows = list((await session.execute(stmt)).scalars().all())
    rows.sort(key=lambda row: (row.occurred_at, str(row.id)))

    used: set[str] = set()
    pairs: list[tuple[Any, Any]] = []
    for row in rows:
        row_id = str(row.id)
        if row_id in used or row.amount >= 0:
            continue
        candidates = [
            candidate
            for candidate in rows
            if str(candidate.id) not in used
            and str(candidate.id) != row_id
            and candidate.account_id != row.account_id
            and candidate.amount == -row.amount
            and abs(candidate.occurred_at - row.occurred_at)
            <= timedelta(seconds=60)
            and candidate.amount > 0
            and (
                (
                    not (row.description or "").strip()
                    and not (candidate.description or "").strip()
                )
                or (
                    (row.description or "").strip().casefold()
                    == (candidate.description or "").strip().casefold()
                    and (row.description or "")
                    .strip()
                    .casefold()
                    .startswith("salaris ")
                )
            )
        ]
        if not candidates:
            continue
        partner = min(
            candidates,
            key=lambda candidate: (
                abs(candidate.occurred_at - row.occurred_at),
                str(candidate.id),
            ),
        )
        used.update({row_id, str(partner.id)})
        pairs.append((row, partner))

    changed = 0
    for row, partner in pairs:
        pair_key = (
            "bunq-easy-budgeting-pair:"
            f"{row.occurred_at.isoformat()}:{abs(row.amount)}:"
            f"{min(row.account_id, partner.account_id)}:"
            f"{max(row.account_id, partner.account_id)}"
        )
        for item in (row, partner):
            metadata = dict(item.provider_metadata or {})
            metadata.update(
                {
                    "internal_transfer": True,
                    "internal_transfer_detection": "bunq_easy_budgeting_pair",
                    "internal_transfer_pair_key": pair_key,
                }
            )
            item.provider_metadata = metadata
            item.transaction_type = "transfer"
            item.export_status = "excluded"
            item.classification_source = "bunq_easy_budgeting_pair"
            item.revision = (item.revision or 0) + 1
            changed += 1
    if changed:
        await session.flush()
    return changed


def normalize_account_reference(value: str | None) -> str:
    """Normalize an account reference for reliable IBAN comparison."""
    return "".join((value or "").upper().split())


def bunq_owned_ibans(accounts: Iterable[object]) -> set[str]:
    """Return IBANs belonging to the Bunq accounts in *accounts*."""
    owned: set[str] = set()
    for account in accounts:
        if str(getattr(account, "provider_key", "")).lower() != "bunq":
            continue
        metadata = getattr(account, "provider_metadata", None) or {}
        iban = normalize_account_reference(metadata.get("iban"))
        if iban:
            owned.add(iban)
    return owned


def classify_bunq_internal_transfers(
    transactions: Iterable[CanonicalTransactionData],
    *,
    owned_ibans: set[str],
) -> list[CanonicalTransactionData]:
    """Mark Bunq payments to another owned IBAN as canonical transfers.

    Bunq Easy Budgeting can expose automatic top-ups and remainder movements
    as ordinary ``PAYMENT`` records.  The counterparty IBAN is the durable
    identity of the other side, so it is safer than matching descriptions or
    Bunq's provider payment type.
    """
    normalized_owned = {
        normalize_account_reference(iban) for iban in owned_ibans if iban
    }
    result: list[CanonicalTransactionData] = []
    for transaction in transactions:
        counterparty = normalize_account_reference(
            transaction.counterparty_account_reference
        )
        metadata = dict(transaction.provider_metadata or {})
        description = (transaction.description or "").strip()
        easy_budgeting_operation: str | None = None
        easy_budgeting_budget: str | None = None
        if transaction.provider_key.lower() == "bunq":
            # Bunq uses PAYMENT_ALLOCATE for the detailed Easy Budgeting
            # representation, but some accounts expose the same operation as
            # a generic BUNQ payment.  The exact provider-generated
            # description is the reliable discriminator in both forms.
            for operation, pattern in _EASY_BUDGETING_PATTERNS:
                match = pattern.match(description)
                if match:
                    easy_budgeting_operation = operation
                    captured_budget = match.group(1)
                    easy_budgeting_budget = (
                        captured_budget.strip()
                        if captured_budget is not None
                        else None
                    )
                    break
        is_internal = transaction.provider_key.lower() == "bunq" and (
            counterparty in normalized_owned
            or easy_budgeting_operation is not None
        )
        if not is_internal or transaction.transaction_type == "transfer":
            result.append(transaction)
            continue

        metadata["internal_transfer"] = True
        metadata["internal_transfer_detection"] = "owned_counterparty_iban"
        contract = transaction.provider_metadata_contract
        if easy_budgeting_operation is not None:
            metadata["internal_transfer_detection"] = "bunq_easy_budgeting"
            metadata["easy_budgeting_operation"] = easy_budgeting_operation
            metadata["easy_budgeting_budget"] = easy_budgeting_budget
            pair_key = (
                "bunq-easy-budgeting:"
                f"{transaction.occurred_at.date()}:"
                f"{description.casefold()}:"
                f"{abs(transaction.amount)}"
            )
            metadata["internal_transfer_pair_key"] = pair_key
            if isinstance(contract, dict):
                fields = dict(contract.get("fields") or {})
                fields["transfer_id"] = pair_key
                fields["pair_scope"] = "cross_account"
                contract = {**contract, "fields": fields}
            elif contract is not None:
                fields = dict(contract.fields)
                fields["transfer_id"] = pair_key
                fields["pair_scope"] = "cross_account"
                contract = contract.model_copy(update={"fields": fields})
        result.append(
            transaction.model_copy(
                update={
                    "transaction_type": "transfer",
                    "provider_metadata": metadata,
                    "provider_metadata_contract": contract,
                    "classification_source": (
                        transaction.classification_source
                        or "bunq_internal_transfer"
                    ),
                }
            )
        )
    return result
