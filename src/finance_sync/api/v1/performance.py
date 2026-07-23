"""Performance analytics REST endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.performance import (
    AttributionResponse,
    BenchmarkComparisonResponse,
    MWRResponse,
    PerformanceService,
    PerformanceSummaryResponse,
    TWRResponse,
)

router = APIRouter(prefix="/performance", tags=["performance"])


def _get_service(session: AsyncSession) -> PerformanceService:
    return PerformanceService(session)


@router.get("", response_model=PerformanceSummaryResponse)
async def get_performance_summary(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    benchmark_security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return a complete performance summary.

    Includes TWR, MWR, benchmark comparison, and attribution analysis
    for the specified date range.
    """
    svc = _get_service(db)
    result = await svc.get_summary(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        benchmark_security_id=benchmark_security_id,
    )
    return result.model_dump()


@router.get("/twr", response_model=TWRResponse)
async def get_time_weighted_return(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    annualized: bool = Query(default=True),
) -> dict[str, Any]:
    """Return Time-Weighted Return (TWR).

    TWR breaks the evaluation period into sub-periods separated by
    external cash flows (deposits / withdrawals), then geometrically
    links the sub-period returns.
    """
    svc = _get_service(db)
    result = await svc.calculate_twr(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        annualized=annualized,
    )
    return result.model_dump()


@router.get("/mwr", response_model=MWRResponse)
async def get_money_weighted_return(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Return Money-Weighted Return (IRR).

    MWR solves for the internal rate of return that equates the
    present value of all cash flows to zero.
    """
    svc = _get_service(db)
    result = await svc.calculate_mwr(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
    )
    return result.model_dump()


@router.get("/benchmark", response_model=BenchmarkComparisonResponse)
async def get_benchmark_comparison(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    benchmark_security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Compare portfolio performance against a benchmark.

    Returns alpha, beta, tracking error, information ratio, and
    correlation between portfolio and benchmark returns.
    """
    svc = _get_service(db)
    result = await svc.benchmark_comparison(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        benchmark_security_id=benchmark_security_id,
    )
    return result.model_dump()


@router.get("/attribution", response_model=AttributionResponse)
async def get_attribution(
    auth: AuthContext = Depends(require_permission("holdings", "read")),
    db: AsyncSession = Depends(get_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    benchmark_security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return Brinson-style performance attribution.

    Decomposes excess return into allocation effect (sector weighting),
    selection effect (security picking), and interaction effect.
    """
    svc = _get_service(db)
    result = await svc.attribution(
        tenant_id=auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        benchmark_security_id=benchmark_security_id,
    )
    return result.model_dump()
