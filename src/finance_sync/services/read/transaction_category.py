"""Provider-supplied transaction category helpers for read responses."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from finance_sync.models.account import Account
from finance_sync.models.credential import Credential
from finance_sync.services.category_options import canonicalize_category


def _nested_category(value: Any) -> str | None:
    if isinstance(value, Mapping):
        category = value.get("category") or value.get("value")
        if category:
            return str(category)
    return None


def transaction_category(
    transaction: Any, account_fallback: str | None = None
) -> str:
    """Return the canonical provider category for a transaction.

    Bunq writes its MCC/merchant-rule result to both provider metadata and
    the structured category suggestion. Read the provider value first so a
    later user classification cannot replace what Bunq supplied. Historical
    Bunq rows without metadata remain visible as ``other`` rather than an
    empty category.
    """
    override = getattr(transaction, "classification_override", None)
    if override:
        return canonicalize_category(str(override)) or str(override)

    metadata = transaction.provider_metadata or {}
    category = _nested_category(metadata)
    if category and category.lower() != "other":
        return canonicalize_category(category) or category

    contract = transaction.provider_metadata_contract or {}
    fields = contract.get("fields") if isinstance(contract, Mapping) else None
    structured_category = _nested_category(fields)
    if structured_category and structured_category.lower() != "other":
        return canonicalize_category(structured_category) or structured_category

    suggested_category = _nested_category(transaction.cashflow_suggestion)
    if suggested_category and suggested_category.lower() != "other":
        return canonicalize_category(suggested_category) or suggested_category

    if account_fallback and account_fallback.strip():
        return (
            canonicalize_category(account_fallback) or account_fallback.strip()
        )

    if category:
        return canonicalize_category(category) or category
    if structured_category:
        return canonicalize_category(structured_category) or structured_category
    if suggested_category:
        return canonicalize_category(suggested_category) or suggested_category

    if transaction.provider_key == "bunq":
        return "other_expenses"
    if transaction.cashflow_bucket:
        return canonicalize_category(str(transaction.cashflow_bucket)) or str(
            transaction.cashflow_bucket
        )
    return "other_expenses"


async def account_category_fallbacks(
    session: Any, tenant_id: str, account_ids: Sequence[str]
) -> dict[str, str]:
    """Resolve configured Bunq fallbacks by canonical account ID."""
    if not account_ids:
        return {}

    result = await session.execute(
        select(
            Account.id,
            Account.external_account_id,
            Credential.description,
        )
        .join(Credential, Credential.id == Account.connection_id)
        .where(
            Account.tenant_id == tenant_id,
            Account.id.in_(list(account_ids)),
            Account.provider_key == "bunq",
        )
    )
    fallbacks: dict[str, str] = {}
    for account_id, external_id, description in result.all():
        with_context: Any = {}
        try:
            parsed = json.loads(description or "{}")
            if isinstance(parsed, dict):
                with_context = parsed.get("account_category_fallbacks", {})
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(with_context, Mapping):
            continue
        value = with_context.get(str(external_id))
        if value and str(value).strip():
            fallbacks[str(account_id)] = str(value).strip()
    return fallbacks
