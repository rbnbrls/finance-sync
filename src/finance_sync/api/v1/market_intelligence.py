"""Market-intelligence read API.

Tenant-scoped read contract for the source layer: source metadata and
structured facts — never provider credentials and never full
(unlicensed) article text.  Restricted items are stored without a body
by the ingestion layer, so this surface can only ever serve what the
licensing policy allowed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container
from finance_sync.services.market_intelligence_catalog import (
    IntelSourceCatalogResponse,
    IntelSourceCatalogService,
)
from finance_sync.services.market_intelligence_read import (
    IntelRunDTO,
    MarketIntelligenceItemDTO,
    MarketIntelligenceListResponse,
    MarketIntelligenceReadService,
    ProviderStateDTO,
    ReviewQueueDTO,
    review_to_dto,
)

router = APIRouter(
    prefix="/market-intelligence",
    tags=["market-intelligence"],
)


def _read_service(request: Request) -> MarketIntelligenceReadService:
    """Create a read service bound to a fresh session from the container."""
    container = get_container(request)
    session = container.session_factory()
    return MarketIntelligenceReadService(session)


# ── Source catalog ────────────────────────────────────────────────────


@router.get(
    "/sources",
    response_model=IntelSourceCatalogResponse,
)
async def list_intel_sources(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
) -> IntelSourceCatalogResponse:
    """Return the source catalog: static metadata of every provider.

    Tenant-scoped (auth-required).  The catalog carries adapter-declared
    provenance, licence terms, configuration links, rate-limit and
    freshness policies and declared capabilities — never provider
    credentials, never raw API responses, never full article text.
    """
    container = get_container(request)
    service = IntelSourceCatalogService(container.intel_registry)
    return await service.catalog()


# ── Items ─────────────────────────────────────────────────────────────


@router.get(
    "/items",
    response_model=MarketIntelligenceListResponse,
)
async def list_intel_items(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
    provider: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    review_required: bool | None = Query(default=None),
    is_stale: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MarketIntelligenceListResponse:
    """List stored market-intelligence observations for the tenant."""
    service = _read_service(request)
    try:
        return await service.list_items(
            auth.tenant_id,
            provider=provider,
            kind=kind,
            review_required=review_required,
            is_stale=is_stale,
            limit=limit,
            offset=offset,
        )
    finally:
        await service._session.aclose()  # type: ignore[reportPrivateUsage]


@router.get("/items/{item_id}", response_model=MarketIntelligenceItemDTO)
async def get_intel_item(
    request: Request,
    item_id: str,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
) -> MarketIntelligenceItemDTO:
    """Return one observation iff it belongs to the caller's tenant.

    Cross-tenant ids return 404 (never leak their existence).
    """
    service = _read_service(request)
    try:
        dto = await service.get_item(auth.tenant_id, item_id)
        if dto is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Observation not found",
            )
        return dto
    finally:
        await service._session.aclose()  # type: ignore[reportPrivateUsage]


# ── Provider state ────────────────────────────────────────────────────


@router.get("/providers", response_model=list[ProviderStateDTO])
async def list_provider_states(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
) -> list[ProviderStateDTO]:
    """Return per-provider run/freshness state for the tenant.

    Includes the sanitised last error (secrets redacted) and the
    explicit availability status per capability.
    """
    service = _read_service(request)
    try:
        return await service.list_provider_states(auth.tenant_id)
    finally:
        await service._session.aclose()  # type: ignore[reportPrivateUsage]


@router.get("/runs", response_model=list[IntelRunDTO])
async def list_intel_runs(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[IntelRunDTO]:
    """Return the recorded scheduler runs for the tenant (run registry).

    Every run is observable: started/completed timestamps, duration,
    quota usage, freshness snapshot and sanitised errors — newest
    first, tenant-scoped.
    """
    service = _read_service(request)
    try:
        return await service.list_runs(
            auth.tenant_id,
            provider=provider,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        await service._session.aclose()  # type: ignore[reportPrivateUsage]


# ── Review queue ──────────────────────────────────────────────────────


@router.get("/review-queue", response_model=list[ReviewQueueDTO])
async def list_review_queue(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ReviewQueueDTO]:
    """Return ambiguous-resolution items awaiting review for the tenant."""
    service = _read_service(request)
    try:
        return await service.list_review_queue(
            auth.tenant_id,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    finally:
        await service._session.aclose()  # type: ignore[reportPrivateUsage]


# ── Review queue: manual resolution ───────────────────────────────────


class ReviewResolveRequest(BaseModel):
    """Body for resolving an ambiguous review-queue entry."""

    security_id: str = Field(
        description="The canonical Security id to link the observation to"
    )
    note: str | None = Field(
        default=None,
        description="Optional free-form note from the reviewer",
    )


class ReviewDismissRequest(BaseModel):
    """Body for dismissing an ambiguous review-queue entry."""

    note: str | None = Field(
        default=None,
        description="Optional free-form note from the reviewer",
    )


@router.post("/review-queue/{entry_id}/resolve", response_model=ReviewQueueDTO)
async def resolve_review_entry(
    request: Request,
    entry_id: str,
    body: ReviewResolveRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> ReviewQueueDTO:
    """Resolve an ambiguous observation to a canonical security.

    Links the underlying observation to *security_id* and marks the
    queue entry resolved.  Tenant-scoped: a cross-tenant entry id is a
    plain 404 (never leaks existence).
    """
    from finance_sync.services.market_intelligence_review import (
        IntelReviewService,
        ReviewQueueError,
    )

    container = get_container(request)
    session = container.session_factory()
    try:
        service = IntelReviewService(session)
        entry = await service.resolve_entry(
            auth.tenant_id,
            entry_id,
            body.security_id,
            note=body.note,
            resolver_principal=auth.principal_id,
        )
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Review entry not found",
            )
        await session.commit()
        return review_to_dto(entry)
    except ReviewQueueError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    finally:
        await session.aclose()


@router.post("/review-queue/{entry_id}/dismiss", response_model=ReviewQueueDTO)
async def dismiss_review_entry(
    request: Request,
    entry_id: str,
    body: ReviewDismissRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> ReviewQueueDTO:
    """Dismiss an ambiguous observation (no security is chosen).

    Marks the queue entry dismissed and clears the review flag on the
    observation; the observation keeps no security link.
    """
    from finance_sync.services.market_intelligence_review import (
        IntelReviewService,
    )

    container = get_container(request)
    session = container.session_factory()
    try:
        service = IntelReviewService(session)
        entry = await service.dismiss_entry(
            auth.tenant_id,
            entry_id,
            note=body.note,
            resolver_principal=auth.principal_id,
        )
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Review entry not found",
            )
        await session.commit()
        return review_to_dto(entry)
    finally:
        await session.aclose()
