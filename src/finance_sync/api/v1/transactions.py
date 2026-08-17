"""Top-level transactions endpoint — ``GET /transactions``.

Lists canonical cash transactions across all of the authenticated
tenant's accounts, with the documented filters (accountId, provider,
status, type, from, to, currency) and the collection ``meta`` envelope.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_read_scope,
    require_permission,
)
from finance_sync.dependencies import get_db
from finance_sync.models.enums import TransactionType
from finance_sync.services.read_api import (
    ReadService,
    TopLevelTransactionListResponse,
)
from finance_sync.services.visibility import ReadScope

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _get_service(
    session: AsyncSession, scope: ReadScope | None = None
) -> ReadService:
    return ReadService(session, scope=scope)


@router.get("", response_model=TopLevelTransactionListResponse)
async def list_transactions(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    scope: ReadScope = Depends(get_read_scope),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: str | None = Query(default=None, alias="accountId"),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    transaction_type: TransactionType | None = Query(
        default=None, alias="type"
    ),
    currency: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    sort_by: str = Query(default="occurred_at"),
    sort_order: str = Query(default="desc"),
) -> dict[str, Any]:
    """List canonical cash transactions for the authenticated tenant.

    Supports the ``docs/API.md`` filters: accountId, provider, status,
    type, from, to, currency.  Returns the collection ``meta`` envelope.
    """
    svc = _get_service(db, scope=scope)
    result = await svc.list_transactions(
        tenant_id=auth.tenant_id,
        account_id=account_id,
        provider_key=provider,
        status=status,
        transaction_type=transaction_type,
        currency_code=currency,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return result.model_dump()
