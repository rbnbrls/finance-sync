"""Read-only aggregate analytics endpoint."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_read_scope,
    require_permission,
)
from finance_sync.dependencies import get_db
from finance_sync.schemas.analytics import AnalyticsOverview
from finance_sync.services.ai_summary import AISummaryService
from finance_sync.services.analytics_overview import AnalyticsOverviewService
from finance_sync.services.visibility import ReadScope

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview", response_model=AnalyticsOverview)
async def get_analytics_overview(
    request: Request,
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    scope: ReadScope = Depends(get_read_scope),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    benchmark_security_id: str | None = Query(default=None),
    include_details: bool = Query(
        default=False,
        description="Include bounded subscription and market-intelligence rows.",
    ),
    include_ai: bool = Query(
        default=False,
        description="Generate an AI summary only when explicitly requested.",
    ),
) -> AnalyticsOverview:
    """Return canonical-data analytics with freshness and coverage metadata."""
    settings = request.app.state._settings
    overview = await AnalyticsOverviewService(
        db,
        scope=scope,
        ai_enabled=settings.ai_enabled,
        ai_configured=settings.ai_api_key is not None,
    ).get_overview(
        auth.tenant_id,
        date_from=date_from,
        date_to=date_to,
        benchmark_security_id=benchmark_security_id,
        include_details=include_details,
    )
    if include_ai:
        if not settings.ai_enabled or settings.ai_api_key is None:
            overview.ai_summary.caveats = [
                "AI-samenvatting is niet beschikbaar: activeer AI en configureer een API-key."
            ]
        else:
            summary = await AISummaryService(
                db, settings, scope=scope
            ).generate_summary(tenant_id=auth.tenant_id)
            overview.ai_summary.summary = summary.summary
            overview.ai_summary.generated_at = summary.generated_at
    return overview
