"""Data-health review queue for unclassified Bunq bank transactions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.models import (
    Account,
    Transaction,
    TransactionLifecycleEvent,
    TransactionOverride,
)
from finance_sync.services.category_options import canonicalize_category
from finance_sync.services.read.transaction_category import (
    account_category_fallbacks,
    transaction_category,
)

router = APIRouter(prefix="/control-plane/data-health", tags=["data-health"])


class CategoryReviewRequest(BaseModel):
    category: str = Field(min_length=1, max_length=64)


def _review_item(transaction: Transaction, account_name: str) -> dict[str, Any]:
    return {
        "id": str(transaction.id),
        "occurred_at": transaction.occurred_at,
        "account": account_name,
        "payee": transaction.merchant_name or transaction.counterparty_name,
        "description": transaction.description,
        "amount": transaction.amount,
        "currency_code": transaction.currency_code,
        "category": "other",
    }


@router.get("/bunq-other-transactions", response_model=dict[str, Any])
async def list_bunq_other_transactions(
    limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return unconfirmed Bunq bank transactions whose effective category is other."""
    result = await db.execute(
        select(Transaction, Account.name)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.tenant_id == auth.tenant_id,
            Transaction.provider_key == "bunq",
            Account.provider_key == "bunq",
            Account.account_type.notin_(("investment", "brokerage")),
            Transaction.transaction_type != "transfer",
            Transaction.classification_override.is_(None),
            Transaction.tombstoned_at.is_(None),
        )
        .order_by(
            func.abs(Transaction.amount).desc(), Transaction.occurred_at.desc()
        )
        .limit(limit * 10)
    )
    rows = result.all()
    fallback_map = await account_category_fallbacks(
        db,
        auth.tenant_id,
        [str(transaction.account_id) for transaction, _ in rows],
    )
    items = []
    for transaction, account_name in rows:
        category = canonicalize_category(
            transaction_category(
                transaction, fallback_map.get(str(transaction.account_id))
            )
        )
        if category == "other_expenses":
            items.append(_review_item(transaction, account_name))
            if len(items) >= limit:
                break
    return {"items": items, "total": len(items)}


@router.post(
    "/bunq-other-transactions/{transaction_id}",
    response_model=dict[str, Any],
)
async def classify_bunq_other_transaction(
    transaction_id: str,
    body: CategoryReviewRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Assign or explicitly confirm the category for one review item."""
    category = canonicalize_category(body.category)
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose one of the available transaction categories.",
        )
    result = await db.execute(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.id == transaction_id,
            Transaction.tenant_id == auth.tenant_id,
            Transaction.provider_key == "bunq",
            Account.provider_key == "bunq",
            Account.account_type.notin_(("investment", "brokerage")),
            Transaction.transaction_type != "transfer",
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )

    source_revision = transaction.revision
    transaction.classification_override = category
    transaction.classification_source = "user_category_review"
    transaction.revision = (transaction.revision or 0) + 1
    actor = str(auth.user.id) if auth.user is not None else None
    db.add(
        TransactionOverride(
            tenant_id=auth.tenant_id,
            transaction_id=transaction.id,
            field_name="classification_override",
            value={"value": category},
            actor=actor,
            provenance="user_override",
        )
    )
    db.add(
        TransactionLifecycleEvent(
            tenant_id=auth.tenant_id,
            transaction_id=transaction.id,
            event_type="update",
            idempotency_key=f"bunq-category-review:{transaction.id}:{transaction.revision}",
            payload={"classification_override": category},
            actor=actor,
            provenance="user_override",
            source_revision=source_revision,
        )
    )
    await db.commit()
    return {"transaction_id": str(transaction.id), "category": category}
