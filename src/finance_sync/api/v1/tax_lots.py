"""REST API endpoints for tax lot data.

Exposes:
- GET /tax-lots — list tax lots with filters
- GET /tax-lots/summary — aggregate summary
- POST /tax-lots/compute — trigger recomputation
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.db.repositories import TaxLotRepository
from finance_sync.dependencies import get_db
from finance_sync.models.tax_lot import TaxLot
from finance_sync.services.tax_lot_service import (
    compute_all_tax_lots,
    get_tax_lot_summary,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/tax-lots", tags=["tax-lots"])


# ── Response DTOs ──────────────────────────────────────────────────────


class TaxLotResponse(BaseModel):
    id: str
    account_id: str
    security_id: str
    quantity: Decimal
    remaining_quantity: Decimal
    cost_basis_total: Decimal
    cost_basis_per_unit: Decimal
    currency_code: str
    acquired_at: datetime
    closed_at: datetime | None = None
    realized_pl: Decimal | None = None
    has_wash_sale_adjustment: bool = False
    disallowed_loss: Decimal | None = None
    is_open: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaxLotListResponse(BaseModel):
    items: list[TaxLotResponse]
    total: int
    limit: int
    offset: int


class TaxLotSummaryResponse(BaseModel):
    total_lots: int
    open_lots: int
    closed_lots: int
    open_cost_basis: Decimal | None = None
    total_realized_pl: Decimal | None = None
    wash_sale_adjusted_lots: int = 0


class ComputeResult(BaseModel):
    status: str
    transactions_processed: int = 0
    lots_created: int = 0
    lots_closed: int = 0
    wash_sale_adjustments: int = 0
    total_realized_pl: Decimal | None = None


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("", response_model=TaxLotListResponse)
async def list_tax_lots(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    account_id: str | None = Query(default=None),
    security_id: str | None = Query(default=None),
    is_open: bool | None = Query(default=None),
) -> dict[str, Any]:
    """List tax lots for the tenant with optional filters."""
    repo = TaxLotRepository(db)
    conditions: list[Any] = [TaxLot.tenant_id == auth.tenant_id]  # type: ignore[attr-defined]

    if account_id:
        conditions.append(TaxLot.account_id == account_id)  # type: ignore[attr-defined]
    if security_id:
        conditions.append(TaxLot.security_id == security_id)  # type: ignore[attr-defined]
    if is_open is True:
        conditions.append(TaxLot.closed_at.is_(None))  # type: ignore[attr-defined]
    elif is_open is False:
        conditions.append(TaxLot.closed_at.isnot(None))  # type: ignore[attr-defined]

    lots = await repo.list(
        *conditions,
        order_by=TaxLot.acquired_at.desc(),  # type: ignore[attr-defined]
        limit=limit,
        offset=offset,
    )

    # Count total without pagination
    total = len(
        await repo.list(
            *conditions,
        )
    )

    return {
        "items": [_lot_to_response(lot) for lot in lots],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/summary", response_model=TaxLotSummaryResponse)
async def tax_lot_summary(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    account_id: str | None = Query(default=None),
    security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return aggregate summary of tax lots."""
    return await get_tax_lot_summary(
        db,
        tenant_id=auth.tenant_id,
        account_id=account_id,
        security_id=security_id,
    )


@router.post("/compute", response_model=ComputeResult)
async def recompute_tax_lots(
    auth: AuthContext = Depends(require_permission("holdings", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recompute all tax lots from scratch for this tenant."""
    # Clear existing tax lots for tenant
    repo = TaxLotRepository(db)
    existing = await repo.list(TaxLot.tenant_id == auth.tenant_id)  # type: ignore[attr-defined]
    for lot in existing:
        await repo.delete(lot)

    stats = await compute_all_tax_lots(db, tenant_id=auth.tenant_id)
    return {
        "status": "completed",
        **stats,
        "total_realized_pl": str(stats.get("total_realized_pl", Decimal(0))),
    }


# ── Helpers ────────────────────────────────────────────────────────────


def _lot_to_response(lot: TaxLot) -> dict[str, Any]:
    return {
        "id": str(lot.id),
        "account_id": str(lot.account_id),
        "security_id": str(lot.security_id),
        "quantity": lot.quantity,
        "remaining_quantity": lot.remaining_quantity,
        "cost_basis_total": lot.cost_basis_total,
        "cost_basis_per_unit": lot.cost_basis_per_unit,
        "currency_code": lot.currency_code,
        "acquired_at": lot.acquired_at,
        "closed_at": lot.closed_at,
        "realized_pl": lot.realized_pl,
        "has_wash_sale_adjustment": lot.has_wash_sale_adjustment,
        "disallowed_loss": lot.disallowed_loss,
        "is_open": lot.is_open(),
        "created_at": lot.created_at,
        "updated_at": lot.updated_at,
    }
