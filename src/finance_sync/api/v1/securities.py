"""Security identity resolution API endpoints.

Provides:
- ``GET /securities/unresolved`` — list securities awaiting manual resolution
- ``POST /securities/resolve`` — manually resolve an unresolved security
- ``PUT /securities/{id}/map`` — map an incoming security to a canonical record
- ``GET /securities/audit-log`` — view resolution decision history

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.identity import (
    AuditLogResponse,
    MapRequest,
    ResolveRequest,
    UnresolvedSecurityResponse,
)

router = APIRouter(prefix="/securities", tags=["securities"])


# ── Response models ───────────────────────────────────────────────────


class UnresolvedListResponse(BaseModel):
    items: list[UnresolvedSecurityResponse]
    total: int


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


class ManualResolveResponse(BaseModel):
    id: str
    target_security_id: str
    resolution_method: str
    detail: str


class MapResponse(BaseModel):
    target_security_id: str
    provider_key: str
    external_security_id: str
    detail: str


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/unresolved", response_model=UnresolvedListResponse)
async def list_unresolved(
    request: Request,
    provider_key: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> UnresolvedListResponse:
    """List securities that could not be auto-resolved and need
    human attention.  Optionally filter by provider_key.

    By default returns only records that have NOT yet been mapped
    (``resolved_security_id IS NULL``).
    """
    container = get_container(request)
    service = container.identity_resolution_service

    unresolved = await service.get_unresolved(
        only_unmapped=True,
        provider_key=provider_key,
        limit=limit,
        offset=offset,
    )

    return UnresolvedListResponse(
        items=[
            UnresolvedSecurityResponse(
                id=u.id,
                provider_key=u.provider_key,
                external_security_id=u.external_security_id,
                raw_isin=u.raw_isin,
                raw_figi=u.raw_figi,
                raw_ticker=u.raw_ticker,
                raw_name=u.raw_name,
                raw_currency_code=u.raw_currency_code,
                raw_metadata=u.raw_metadata,
                resolved_security_id=u.resolved_security_id,
                resolution_method=u.resolution_method,
                resolution_notes=u.resolution_notes,
                created_at=u.created_at,
                updated_at=u.updated_at,
            )
            for u in unresolved
        ],
        total=len(unresolved),
    )


@router.get("/unresolved/all", response_model=UnresolvedListResponse)
async def list_all_unresolved(
    request: Request,
    provider_key: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> UnresolvedListResponse:
    """List ALL securities that entered the manual queue, including
    those already resolved.  Optionally filter by provider_key.
    """
    container = get_container(request)
    service = container.identity_resolution_service

    unresolved = await service.get_unresolved(
        only_unmapped=False,
        provider_key=provider_key,
        limit=limit,
        offset=offset,
    )

    return UnresolvedListResponse(
        items=[
            UnresolvedSecurityResponse(
                id=u.id,
                provider_key=u.provider_key,
                external_security_id=u.external_security_id,
                raw_isin=u.raw_isin,
                raw_figi=u.raw_figi,
                raw_ticker=u.raw_ticker,
                raw_name=u.raw_name,
                raw_currency_code=u.raw_currency_code,
                raw_metadata=u.raw_metadata,
                resolved_security_id=u.resolved_security_id,
                resolution_method=u.resolution_method,
                resolution_notes=u.resolution_notes,
                created_at=u.created_at,
                updated_at=u.updated_at,
            )
            for u in unresolved
        ],
        total=len(unresolved),
    )


@router.post("/resolve", response_model=ManualResolveResponse)
async def resolve_security(
    body: ResolveRequest,
    request: Request,
) -> ManualResolveResponse:
    """Manually resolve an unresolved security by linking it to
    a canonical Security record.

    Requires the ``id`` of the ``UnresolvedSecurity`` record and
    the ``target_security_id`` of the canonical ``Security``.
    """
    container = get_container(request)
    service = container.identity_resolution_service

    result = await service.manually_resolve(
        unresolved_id=body.unresolved_security_id,
        target_security_id=body.target_security_id,
        resolver_principal="api:user",
        resolution_notes=body.resolution_notes,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unresolved security record or target security not found",
        )

    return ManualResolveResponse(
        id=result.id,
        target_security_id=result.target_security_id,
        resolution_method=result.resolution_method,
        detail="Security resolved successfully, background "
        "enrichment triggered",
    )


@router.put("/map", response_model=MapResponse)
async def map_security(
    body: MapRequest,
    request: Request,
) -> MapResponse:
    """Map a specific incoming security (by provider key + external ID)
    to a canonical security record.

    This is the direct-link endpoint: you don't need the ``UnresolvedSecurity``
    record ID, just the provider key and external security ID.
    """
    container = get_container(request)
    service = container.identity_resolution_service

    result = await service.map_and_resolve(
        provider_key=body.provider_key,
        external_security_id=body.external_security_id,
        target_security_id=body.target_security_id,
        resolver_principal="api:user",
        resolution_notes=body.resolution_notes,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target security not found",
        )

    return MapResponse(
        target_security_id=result.target_security_id,
        provider_key=body.provider_key,
        external_security_id=body.external_security_id,
        detail="Security mapped successfully, background enrichment triggered",
    )


@router.get("/audit-log", response_model=AuditLogListResponse)
async def list_audit_log(
    request: Request,
    target_security_id: str | None = None,
    limit: int = 100,
) -> AuditLogListResponse:
    """List resolution audit log entries, optionally filtered by
    target security.
    """
    container = get_container(request)
    service = container.identity_resolution_service

    entries = await service.get_audit_log(
        target_security_id=target_security_id,
        limit=limit,
    )

    return AuditLogListResponse(
        items=[
            AuditLogResponse(
                id=e.id,
                unresolved_security_id=e.unresolved_security_id,
                source_security_id=e.source_security_id,
                target_security_id=e.target_security_id,
                resolution_method=e.resolution_method,
                confidence=e.confidence,
                resolver_principal=e.resolver_principal,
                resolved_at=e.resolved_at,
                resolution_detail=e.resolution_detail,
                match_score=e.match_score,
                created_at=e.created_at,
            )
            for e in entries
        ],
        total=len(entries),
    )


_require_securities_read = require_permission("securities", "read")


@router.get("")
async def list_securities(
    request: Request,  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(_require_securities_read),  # noqa: ARG001
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    security_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    """List known securities with optional type and text search filters.

    Each security includes its latest available price.
    """
    from finance_sync.services.read_api import ReadService

    svc = ReadService(db)
    result = await svc.list_securities(
        limit=limit,
        offset=offset,
        security_type=security_type,
        search=search,
    )
    return result.model_dump()


@router.get("/{security_id}/prices")
async def get_security_prices(
    security_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(_require_securities_read),  # noqa: ARG001
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    interval: str = Query(default="1d"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """List price observations (OHLCV) for a security.

    Supports date range filtering and interval selection.
    """
    from finance_sync.services.read_api import ReadService

    svc = ReadService(db)
    result = await svc.get_security_prices(
        security_id=security_id,
        limit=limit,
        offset=offset,
        interval=interval,
        date_from=date_from,
        date_to=date_to,
    )
    return result.model_dump()
