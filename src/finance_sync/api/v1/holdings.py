"""Top-level holdings endpoint — ``GET /holdings``.

Returns aggregated current holdings (latest snapshot per account +
security) for the authenticated tenant, with optional account/security
and as-of filters and the collection ``meta`` envelope.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import HoldingsListResponse, ReadService

router = APIRouter(prefix="/holdings", tags=["holdings"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=HoldingsListResponse)
async def list_holdings(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: str | None = Query(default=None, alias="accountId"),
    security_id: str | None = Query(default=None, alias="securityId"),
    as_of: datetime | None = Query(default=None, alias="asOf"),
) -> dict[str, Any]:
    """List the tenant's latest (or as-of) aggregated holdings."""
    svc = _get_service(db)
    result = await svc.get_holdings(
        tenant_id=auth.tenant_id,
        account_id=account_id,
        security_id=security_id,
        as_of=as_of,
        limit=limit,
        offset=offset,
    )
    return result.model_dump()
