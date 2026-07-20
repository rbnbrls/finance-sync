"""Root health-check endpoint.

NOTE: ``from __future__ import annotations`` is intentionally omitted here
because FastAPI needs to introspect the function signatures at runtime
when generating OpenAPI schemas.
"""

from fastapi import APIRouter, Depends

from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_settings

router = APIRouter(tags=["health"])


@router.get("/")
async def root(
    settings: Settings = Depends(get_settings),
) -> dict[str, str | bool]:
    """Return API status and version."""
    return {
        "status": "ok",
        "version": settings.app_version,
    }
