"""Top-level dividends endpoint — ``GET /dividends``.

Returns dividend-type transactions across the authenticated tenant's
accounts, with optional account/security and date filters and the
collection ``meta`` envelope.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import DividendListResponse, ReadService

router = APIRouter(prefix="/dividends", tags=["dividends"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=DividendListResponse)
async def list_dividends(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: str | None = Query(default=None, alias="accountId"),
    security_id: str | None = Query(default=None, alias="securityId"),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    """List dividend records for the authenticated tenant."""
    svc = _get_service(db)
    result = await svc.list_dividends(
        tenant_id=auth.tenant_id,
        account_id=account_id,
        security_id=security_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return result.model_dump()
