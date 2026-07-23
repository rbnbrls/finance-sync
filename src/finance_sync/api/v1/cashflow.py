"""Cashflow REST endpoint — uses CashflowService for computation.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.cashflow import (
    CashflowService,
    CashflowSummary,
    CategoryBreakdown,
    PeriodEntry,
)

router = APIRouter(prefix="/cashflow", tags=["cashflow"])

# ── Response models ────────────────────────────────────────────────────


class CashflowPaginatedResponse(BaseModel):
    """Cashflow report with paginated period history.

    Combines the aggregate summary, category breakdown, and a paginated
    list of period-based cashflow entries (time-series).
    """

    summary: CashflowSummary
    by_category: list[CategoryBreakdown] = Field(default_factory=list)
    items: list[PeriodEntry] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


# ── Helpers ────────────────────────────────────────────────────────────


def _get_service(session: AsyncSession) -> CashflowService:
    return CashflowService(session)


_INTERVAL_MAP: dict[str, str] = {
    "day": "day",
    "week": "week",
    "month": "month",
    "year": "year",
}


def _resolve_interval(interval: str | None) -> str:
    """Return the validated interval string, defaulting to 'month'."""
    if interval is None:
        return "month"
    if interval.lower() in _INTERVAL_MAP:
        return interval.lower()
    valid = ", ".join(_INTERVAL_MAP)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"Invalid interval '{interval}'. Must be one of: {valid}",
    )


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("", response_model=CashflowPaginatedResponse)
async def get_cashflow(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    start_date: datetime | None = Query(
        default=None,
        description=(
            "Start of date range (ISO-8601). "
            "Defaults to 365 days before end_date."
        ),
    ),
    end_date: datetime | None = Query(
        default=None,
        description="End of date range (ISO-8601). Defaults to now.",
    ),
    account_ids: list[str] | None = Query(
        default=None,
        description=(
            "Optional list of account UUIDs to filter by. "
            "Repeatable query parameter."
        ),
    ),
    category: str | None = Query(
        default=None,
        description=(
            "Filter by transaction type / category (e.g. 'deposit', 'payment')."
        ),
    ),
    interval: str | None = Query(
        default=None,
        description=(
            "Period interval for time-series entries: "
            "'day', 'week', 'month', 'year'. Defaults to 'month'."
        ),
    ),
    limit: int = Query(
        default=12,
        ge=1,
        le=365,
        description="Maximum number of period entries to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of period entries to skip.",
    ),
) -> dict[str, Any]:
    """Return cashflow report with summary, category breakdown,
    and paginated period history.

    - **Summary**: aggregate inflows, outflows, net cashflow for the period
    - **Category breakdown**: inflow/outflow grouped by transaction type
    - **Period entries**: paginated time-series with per-period in/out/net

    Positive transaction amounts count as inflows; negative amounts as outflows.
    Only **booked** transactions are included.
    """
    # ── Validate date range ────────────────────────────────────────
    try:
        CashflowService.validate_date_range(start_date, end_date)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from None

    resolved_interval = _resolve_interval(interval)
    svc = _get_service(db)

    # ── Summary ────────────────────────────────────────────────────
    try:
        summary = await svc.calculate(
            tenant_id=auth.tenant_id,
            date_from=start_date,
            date_to=end_date,
            account_ids=account_ids,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute cashflow summary: {e}",
        ) from None

    # ── Category breakdown ─────────────────────────────────────────
    try:
        categories = await svc.by_category(
            tenant_id=auth.tenant_id,
            date_from=start_date,
            date_to=end_date,
            transaction_type=category,
            account_ids=account_ids,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute category breakdown: {e}",
        ) from None

    # ── Paginated period entries ───────────────────────────────────
    try:
        items = await svc.by_period(
            tenant_id=auth.tenant_id,
            date_from=start_date,
            date_to=end_date,
            transaction_type=category,
            interval=resolved_interval,
            account_ids=account_ids,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch period entries: {e}",
        ) from None

    # ── Total count (distinct periods) ─────────────────────────────
    try:
        total = await svc.count_periods(
            tenant_id=auth.tenant_id,
            date_from=start_date,
            date_to=end_date,
            transaction_type=category,
            interval=resolved_interval,
            account_ids=account_ids,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to count periods: {e}",
        ) from None

    return {
        "summary": summary.model_dump(),
        "by_category": [c.model_dump() for c in categories],
        "items": [i.model_dump() for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
