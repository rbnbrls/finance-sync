"""Top-level prices endpoint — ``GET /prices``.

Returns either the price series for one security/listing (with
interval + date-range filters) or, without a security filter, the
latest price observation per security.  Carries the collection
``meta`` envelope.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import (
    ReadService,
    TopLevelPriceListResponse,
)

router = APIRouter(prefix="/prices", tags=["prices"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=TopLevelPriceListResponse)
async def get_prices(
    auth: AuthContext = Depends(require_permission("securities", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    security_id: str | None = Query(default=None, alias="securityId"),
    listing_id: str | None = Query(default=None, alias="listingId"),
    interval: str = Query(default="1d"),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    """List latest or historical prices.

    Pass ``securityId`` or ``listingId`` for one security's price
    series; omit both to get the latest price per security.
    """
    svc = _get_service(db)
    resolved_security_id = security_id
    if resolved_security_id is None and listing_id is not None:
        resolved_security_id = await svc.resolve_listing_security_id(listing_id)
        if resolved_security_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Listing not found",
            )
    result = await svc.get_prices(
        security_id=resolved_security_id,
        interval=interval,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return result.model_dump()
