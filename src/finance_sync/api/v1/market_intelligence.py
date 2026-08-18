"""Market-intelligence read API.

Tenant-scoped read contract for the source layer: source metadata and
structured facts — never provider credentials and never full
(unlicensed) article text.  Restricted items are stored without a body
by the ingestion layer, so this surface can only ever serve what the
licensing policy allowed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container
from finance_sync.services.market_intelligence_read import (
    MarketIntelligenceItemDTO,
    MarketIntelligenceListResponse,
    MarketIntelligenceReadService,
    ProviderStateDTO,
    ReviewQueueDTO,
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
