"""Control-plane overview endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.schemas.control_plane import ControlPlaneOverview
from finance_sync.schemas.data_health import DataHealthOverview
from finance_sync.schemas.data_quality import DataQualityOverview
from finance_sync.schemas.provider_health import ProviderHealthOverview
from finance_sync.schemas.remediation import (
    RemediationBulkRetryRequest,
    RemediationEnqueueResponse,
    RemediationIgnoreRequest,
    RemediationItemResponse,
    RemediationListResponse,
    RemediationPriorityPatch,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.data_health import DataHealthService
from finance_sync.services.data_quality import DataQualityService
from finance_sync.services.provider_health import ProviderHealthService
from finance_sync.services.remediation import RemediationService

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
        wealthfolio_health_bridge_enabled=getattr(
            settings, "wealthfolio_health_bridge_enabled", False
        ),
    ).get_overview()


@router.post("/data-health/repair")
async def repair_data_health(
    request: Request,
    auth: AuthContext = Depends(require_permission("enrichment", "write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Run safe, deterministic quality repairs for the current tenant.

    Broker activity is never invented: unresolved cost basis, transfers and
    missing transactions stay visible in the Data Health projection.
    """
    items = await RemediationService(
        db, auth.tenant_id
    ).enqueue_data_health_repairs()
    return Response(
        content=RemediationEnqueueResponse(
            enqueued=len(items), item_ids=[str(item.id) for item in items]
        ).model_dump_json(),
        status_code=status.HTTP_202_ACCEPTED,
        media_type="application/json",
    )


@router.post("/wealthfolio/health-sync")
async def trigger_wealthfolio_health_sync(
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "write")),
) -> dict[str, Any]:
    """Run one bounded, tenant-scoped health bridge poll."""
    from finance_sync.worker.jobs import wealthfolio_health_sync_job

    result = await wealthfolio_health_sync_job(
        get_container(request), tenant_id=auth.tenant_id
    )
    return {"accepted": True, **result}


@router.get("/remediation", response_model=RemediationListResponse)
async def list_remediation(
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    provider: str | None = None,
    issue_type: str | None = None,
    severity: str | None = None,
    connection_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RemediationListResponse:
    items = await RemediationService(db, auth.tenant_id).list(
        status=status_filter,
        provider_key=provider,
        issue_type=issue_type,
        severity=severity,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
    )
    return RemediationListResponse(
        items=[RemediationItemResponse.model_validate(item) for item in items],
        limit=limit,
        offset=offset,
    )


@router.post("/remediation/backfill", response_model=RemediationEnqueueResponse)
async def backfill_remediation(
    limit: int = Query(default=500, ge=1, le=500),
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationEnqueueResponse:
    """Backfill only explicitly contract-qualified historical findings."""
    items = await RemediationService(
        db, auth.tenant_id
    ).backfill_reconciliation_findings(limit=limit)
    return RemediationEnqueueResponse(
        enqueued=len(items), item_ids=[str(item.id) for item in items]
    )


@router.get("/remediation/{item_id}", response_model=RemediationItemResponse)
async def get_remediation(
    item_id: str,
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    db: AsyncSession = Depends(get_db),
) -> RemediationItemResponse:
    item = await RemediationService(db, auth.tenant_id).get(item_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail="remediation item not found"
        )
    return RemediationItemResponse.model_validate(item)


@router.post(
    "/remediation/{item_id}/retry", response_model=RemediationItemResponse
)
async def retry_remediation(
    item_id: str,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationItemResponse:
    service = RemediationService(db, auth.tenant_id)
    if not await service.requeue(
        item_id,
        actor_user_id=auth.principal_id,
        actor_role=getattr(auth.user, "role", None),
    ):
        raise HTTPException(
            status_code=404, detail="remediation item not found"
        )
    return await service.get(item_id)  # type: ignore[return-value]


@router.post(
    "/remediation/bulk-retry", response_model=RemediationEnqueueResponse
)
async def bulk_retry_remediation(
    body: RemediationBulkRetryRequest,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationEnqueueResponse:
    item_ids = await RemediationService(db, auth.tenant_id).bulk_requeue(
        body.item_ids,
        actor_user_id=auth.principal_id,
        actor_role=getattr(auth.user, "role", None),
    )
    return RemediationEnqueueResponse(enqueued=len(item_ids), item_ids=item_ids)


@router.post(
    "/remediation/{item_id}/requeue", response_model=RemediationItemResponse
)
async def requeue_remediation(
    item_id: str,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationItemResponse:
    return await retry_remediation(item_id, auth, db)


@router.post(
    "/remediation/{item_id}/ignore", response_model=RemediationItemResponse
)
async def ignore_remediation(
    item_id: str,
    body: RemediationIgnoreRequest,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationItemResponse:
    service = RemediationService(db, auth.tenant_id)
    if not await service.ignore(
        item_id,
        body.reason,
        actor_user_id=auth.principal_id,
        actor_role=getattr(auth.user, "role", None),
    ):
        raise HTTPException(
            status_code=404, detail="remediation item not found"
        )
    return await service.get(item_id)  # type: ignore[return-value]


@router.patch("/remediation/{item_id}", response_model=RemediationItemResponse)
async def patch_remediation(
    item_id: str,
    body: RemediationPriorityPatch,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
    db: AsyncSession = Depends(get_db),
) -> RemediationItemResponse:
    service = RemediationService(db, auth.tenant_id)
    if not await service.patch(
        item_id,
        priority=body.priority,
        status=body.status,
        actor_user_id=auth.principal_id,
        actor_role=getattr(auth.user, "role", None),
    ):
        raise HTTPException(
            status_code=404,
            detail="remediation item not found or no mutable fields",
        )
    return await service.get(item_id)  # type: ignore[return-value]


@router.get("/provider-health", response_model=list[ProviderHealthOverview])
async def get_provider_health_overview(
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[ProviderHealthOverview]:
    """Return connection, resource and processing health per provider."""
    return await ProviderHealthService(db, auth.tenant_id).get_overview()
