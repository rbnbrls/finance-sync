"""Read-only cashflow endpoints — transaction-based cash flow analysis.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import (
    CashflowHistoryResponse,
    CashflowResponse,
    ReadService,
)

router = APIRouter(prefix="/cashflow", tags=["cashflow"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=CashflowResponse)
async def get_cashflow(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    account_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return aggregate cash flow for a given period.

    Uses booked transactions only. Positive amounts are counted as
    inflows, negative amounts as outflows. Returns total inflows,
    total outflows, and net cashflow for the period.
    """
    svc = _get_service(db)
    result = await svc.get_cashflow(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    return result.model_dump()


@router.get("/history", response_model=CashflowHistoryResponse)
async def get_cashflow_history(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=90, ge=1, le=730),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    account_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return cash flow over time (daily buckets).

    Aggregates booked transactions by day to show how inflows,
    outflows, and net cashflow changed over time.
    """
    svc = _get_service(db)
    result = await svc.get_cashflow_history(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    return result.model_dump()
