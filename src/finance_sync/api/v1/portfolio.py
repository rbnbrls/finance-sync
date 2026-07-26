"""Read-only portfolio endpoints — current portfolio and history.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import (
    PortfolioHistoryResponse,
    PortfolioResponse,
    ReadService,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=PortfolioResponse)
async def get_portfolio(
    request: Request,  # noqa: ARG001
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the current portfolio view.

    Shows latest holding snapshots per account and per security,
    enriched with the latest available prices and unrealised P&L.
    """
    svc = _get_service(db)
    result = await svc.get_portfolio(tenant_id=auth.tenant_id)
    return result.model_dump()


@router.get("/history", response_model=PortfolioHistoryResponse)
async def get_portfolio_history(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=90, ge=1, le=730),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Return portfolio value over time.

    Aggregates holding market_value by day to show how total
    investment portfolio value changed over time.
    """
    svc = _get_service(db)
    result = await svc.get_portfolio_history(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
    )
    return result.model_dump()
