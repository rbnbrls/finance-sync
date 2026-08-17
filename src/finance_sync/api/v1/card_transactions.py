"""Read-only card-transaction endpoints.

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
from finance_sync.services.read_api import (
    CardTransactionListResponse,
    ReadService,
)
from finance_sync.services.visibility import ReadScope

router = APIRouter(prefix="/card-transactions", tags=["card-transactions"])


# ── GET /v1/card-transactions ────────────────────────────────────────


@router.get("", response_model=CardTransactionListResponse)
async def list_card_transactions(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    scope: ReadScope = Depends(get_read_scope),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="occurred_at"),
    sort_order: str = Query(default="desc"),
    account_id: str | None = Query(default=None),
    provider_key: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """List card transactions with optional account/provider filters."""
    svc = ReadService(db, scope=scope)
    result = await svc.list_card_transactions(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        account_id=account_id,
        provider_key=provider_key,
        date_from=date_from,
        date_to=date_to,
    )
    return result.model_dump()
