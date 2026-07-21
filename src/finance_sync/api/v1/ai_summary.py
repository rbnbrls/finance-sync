"""AI-powered financial summary endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.api.middleware.ai_rate_limit import check_ai_rate_limit
from finance_sync.dependencies import get_db, get_settings
from finance_sync.services.ai_summary import AISummaryService

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings

router = APIRouter(prefix="/ai", tags=["ai-summary"])


def _require_ai_enabled(request: Request) -> None:
    """FastAPI dependency: ensure AI feature is enabled.

    Place before the auth dependency so it runs first.
    """
    settings: Settings = get_settings(request)
    if not settings.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "AI summary features are disabled."
                " Set AI_ENABLED=true to enable."
            ),
        )


def _get_service(session: AsyncSession, request: Request) -> AISummaryService:
    """Build an AISummaryService from the container settings."""
    settings = get_settings(request)
    return AISummaryService(session, settings)


@router.post("/summary")
async def generate_summary(
    request: Request,
    _: None = Depends(_require_ai_enabled),
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(check_ai_rate_limit),
    time_period_days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Number of days of history to include in the summary.",
    ),
    force_refresh: bool = Query(
        default=False,
        description="Bypass cache and regenerate.",
    ),
) -> dict[str, Any]:
    """Generate a natural-language summary of recent financial activity."""
    svc = _get_service(db, request)
    try:
        result = await svc.generate_summary(
            tenant_id=auth.tenant_id,
            time_period_days=time_period_days,
            force_refresh=force_refresh,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return result.to_dict()


@router.post("/summary/daily")
async def generate_daily_briefing(
    request: Request,
    _: None = Depends(_require_ai_enabled),
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(check_ai_rate_limit),
    force_refresh: bool = Query(
        default=False,
        description="Bypass cache and regenerate.",
    ),
) -> dict[str, Any]:
    """Generate an automated daily financial briefing."""
    svc = _get_service(db, request)
    try:
        result = await svc.generate_daily_briefing(
            tenant_id=auth.tenant_id,
            force_refresh=force_refresh,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return result.to_dict()
