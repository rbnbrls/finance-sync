"""
Allocation API endpoints - portfolio breakdowns by asset class,
sector, and region.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.allocation import (
    AllocationResponse,
    AllocationService,
)

router = APIRouter(prefix="/allocation", tags=["allocation"])


def _get_service(session: AsyncSession) -> AllocationService:
    """Factory for AllocationService."""
    return AllocationService(session)


@router.get("", response_model=AllocationResponse)
async def get_allocation(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    target_currency: str | None = Query(
        default=None,
        description=(
            "Optional ISO-4217 currency code to normalise all values into. "
            "When omitted, values are reported in their native currency "
            "(may be mixed)."
        ),
        pattern=r"^[A-Z]{3}$",
    ),
    account_id: str | None = Query(
        default=None,
        description="Optional account ID to scope the allocation to.",
    ),
) -> dict[str, Any]:
    """Return portfolio allocation breakdowns.

    Computes how the tenant's portfolio is distributed across:
    - **Asset class** (stock, etf, bond, crypto, etc.)
    - **Sector** (Technology, Healthcare, Financials, etc.)
    - **Region** (North America, Europe, Asia Pacific, etc.)

    When ``target_currency`` is set, all holding market values are
    converted to that currency using the latest available FX rates.
    """
    svc = _get_service(db)
    result = await svc.get_allocation(
        tenant_id=auth.tenant_id,
        target_currency=target_currency,
        account_id=account_id,
    )
    return result.model_dump()
