"""Read-only scheduled-payment endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import (
    ReadService,
    ScheduledPaymentListResponse,
)

router = APIRouter(prefix="/scheduled-payments", tags=["scheduled-payments"])


# ── GET /v1/scheduled-payments ───────────────────────────────────────


@router.get("", response_model=ScheduledPaymentListResponse)
async def list_scheduled_payments(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="next_execution_date"),
    sort_order: str = Query(default="desc"),
    account_id: str | None = Query(default=None),
    provider_key: str | None = Query(default=None),
) -> dict[str, Any]:
    """List scheduled payments with optional account/provider filters."""
    svc = ReadService(db)
    result = await svc.list_scheduled_payments(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        account_id=account_id,
        provider_key=provider_key,
    )
    return result.model_dump()
