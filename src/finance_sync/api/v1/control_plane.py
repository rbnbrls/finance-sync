"""Control-plane overview endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.schemas.control_plane import ControlPlaneOverview
from finance_sync.schemas.data_health import DataHealthOverview
from finance_sync.schemas.data_quality import DataQualityOverview
from finance_sync.schemas.provider_health import ProviderHealthOverview
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.data_health import DataHealthService
from finance_sync.services.data_quality import DataQualityService
from finance_sync.services.provider_health import ProviderHealthService

router = APIRouter(prefix="/control-plane", tags=["control-plane"])


@router.get("/overview", response_model=ControlPlaneOverview)
async def get_control_plane_overview(
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> ControlPlaneOverview:
    """Return the tenant's current operational data-flow overview."""
    settings = get_container(request).settings
    return await ControlPlaneService(
        db,
        auth.tenant_id,
        permissions=auth.permissions,
        redis_configured=settings.redis_url is not None,
    ).get_overview()


@router.get("/data-quality", response_model=DataQualityOverview)
async def get_data_quality_overview(
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    db: AsyncSession = Depends(get_db),
) -> DataQualityOverview:
    """Return tenant-scoped reconciliation findings and source coverage."""
    return await DataQualityService(db, auth.tenant_id).get_overview()


@router.get("/data-health", response_model=DataHealthOverview)
async def get_data_health_overview(
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> DataHealthOverview:
    """Return the canonical, actionable Data health projection."""
    settings = get_container(request).settings
    return await DataHealthService(
        db,
        auth.tenant_id,
        permissions=auth.permissions,
        redis_configured=settings.redis_url is not None,
    ).get_overview()


@router.get("/provider-health", response_model=list[ProviderHealthOverview])
async def get_provider_health_overview(
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[ProviderHealthOverview]:
    """Return connection, resource and processing health per provider."""
    return await ProviderHealthService(db, auth.tenant_id).get_overview()
