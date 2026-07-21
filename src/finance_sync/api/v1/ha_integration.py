"""Home Assistant integration endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db, get_settings
from finance_sync.services.ha_integration import HomeAssistantService

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings

router = APIRouter(prefix="/ha", tags=["home-assistant"])


def _require_ha_enabled(request: Request) -> None:
    """FastAPI dependency: ensure HA feature is enabled.

    Place before the auth dependency so it runs first.
    """
    settings: Settings = get_settings(request)
    if not settings.ha_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Home Assistant integration is disabled."
                " Set HA_ENABLED=true to enable."
            ),
        )


def _get_service(
    session: AsyncSession, request: Request
) -> HomeAssistantService:
    """Build a HomeAssistantService from the container settings."""
    settings = get_settings(request)
    return HomeAssistantService(session, settings)


@router.get("/sensors")
async def get_ha_sensors(
    request: Request,
    _: None = Depends(_require_ha_enabled),
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Expose financial sensors for Home Assistant REST sensor integration."""
    svc = _get_service(db, request)
    sensors = await svc.get_sensors(tenant_id=auth.tenant_id)
    return {"sensors": [s.to_dict() for s in sensors]}


@router.get("/config")
async def get_ha_config(
    request: Request,
    _: None = Depends(_require_ha_enabled),
    _auth: AuthContext = Depends(require_permission("accounts", "read")),
) -> dict:
    """Return Home Assistant REST sensor integration configuration."""
    settings = get_settings(request)
    svc = HomeAssistantService(session=None, settings=settings)  # type: ignore[arg-type]
    base_url = str(request.base_url).rstrip("/") + "/api/v1/ha/sensors"
    result = svc.get_config(base_url=base_url)
    return result.to_dict()
