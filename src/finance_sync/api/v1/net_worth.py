"""Read-only net-worth endpoints — current net worth and history.

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
    NetWorthHistoryResponse,
    NetWorthResponse,
    ReadService,
)

router = APIRouter(prefix="/net-worth", tags=["net-worth"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=NetWorthResponse)
async def get_net_worth(
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return aggregate net worth across all accounts.

    Computes total assets and liabilities from the latest
    current_balance on each active account.
    """
    svc = _get_service(db)
    result = await svc.get_net_worth(tenant_id=auth.tenant_id)
    return result.model_dump()


@router.get("/history", response_model=NetWorthHistoryResponse)
async def get_net_worth_history(
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=90, ge=1, le=730),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Return net worth over time.

    Uses balance snapshots (booked/available) aggregated by day
    across all accounts to show net worth history.
    """
    svc = _get_service(db)
    result = await svc.get_net_worth_history(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
    )
    return result.model_dump()
