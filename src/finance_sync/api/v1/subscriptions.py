"""Subscription detection API endpoints — run detection and manage results.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.services.subscription_detector import SubscriptionDetector

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# ── Request / Response DTOs ───────────────────────────────────────────


class DetectionTriggerRequest(BaseModel):
    """Request body to trigger subscription detection."""

    date_from: datetime | None = Field(
        default=None,
        description="Earliest transaction date (default 365 days ago)",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Latest transaction date (default now)",
    )
    min_occurrences: int = Field(
        default=2,
        ge=2,
        le=24,
        description="Minimum occurrences to consider a pattern",
    )


class SubscriptionResponse(BaseModel):
    """Public representation of a detected subscription."""

    id: str
    merchant_name: str
    raw_description: str | None = None
    amount: Decimal
    currency_code: str = "EUR"
    frequency_days: int | None = None
    frequency_label: str | None = None
    confidence: str
    detection_method: str
    status: str
    account_id: str | None = None
    provider_key: str | None = None
    sector: str | None = None
    category: str | None = None
    security_id: str | None = None
    first_detected_at: datetime
    last_detected_at: datetime
    occurrence_count: int
    detection_score: float | None = None
    details: dict[str, Any] | None = None
    user_notes: str | None = None
    created_at: datetime | None = None


class SubscriptionListResponse(BaseModel):
    """List of detected subscriptions."""

    items: list[SubscriptionResponse]
    total: int
    limit: int
    offset: int


class SubscriptionUpdateRequest(BaseModel):
    """Request body to update a detected subscription."""

    status: str | None = Field(
        default=None,
        description="New status: active, paused, cancelled",
    )
    category: str | None = Field(
        default=None,
        description="Override subscription category",
    )
    user_notes: str | None = Field(
        default=None,
        description="User notes or label",
    )


# ── Helpers ───────────────────────────────────────────────────────────


def _sub_to_response(sub: object) -> SubscriptionResponse:
    """Convert an ORM DetectedSubscription to its response DTO."""
    return SubscriptionResponse(
        id=str(getattr(sub, "id", "")),
        merchant_name=str(getattr(sub, "merchant_name", "")),
        raw_description=getattr(sub, "raw_description", None),
        amount=getattr(sub, "amount", Decimal(0)),
        currency_code=str(getattr(sub, "currency_code", "EUR")),
        frequency_days=getattr(sub, "frequency_days", None),
        frequency_label=getattr(sub, "frequency_label", None),
        confidence=str(getattr(sub, "confidence", "")),
        detection_method=str(getattr(sub, "detection_method", "")),
        status=str(getattr(sub, "status", "")),
        account_id=str(getattr(sub, "account_id", ""))
        if getattr(sub, "account_id", None)
        else None,
        provider_key=getattr(sub, "provider_key", None),
        sector=getattr(sub, "sector", None),
        category=getattr(sub, "category", None),
        security_id=str(getattr(sub, "security_id", ""))
        if getattr(sub, "security_id", None)
        else None,
        first_detected_at=getattr(sub, "first_detected_at", datetime.now(UTC)),
        last_detected_at=getattr(sub, "last_detected_at", datetime.now(UTC)),
        occurrence_count=getattr(sub, "occurrence_count", 0),
        detection_score=getattr(sub, "detection_score", None),
        details=getattr(sub, "details", None),
        user_notes=getattr(sub, "user_notes", None),
        created_at=getattr(sub, "created_at", None),
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post(
    "/detect",
    response_model=list[SubscriptionResponse],
    status_code=status.HTTP_200_OK,
)
async def detect_subscriptions(
    body: DetectionTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> list[dict[str, Any]]:
    """Run subscription detection on transaction history.

    Analyzes outgoing transactions for recurring patterns and returns
    newly detected subscriptions.
    """
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    subscriptions = await svc.detect(
        date_from=body.date_from,
        date_to=body.date_to,
        min_occurrences=body.min_occurrences,
    )

    return [_sub_to_response(s).model_dump() for s in subscriptions]


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    auth: AuthContext = Depends(require_permission("subscriptions", "read")),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(
        default=None, alias="status", description="Filter by status"
    ),
    confidence: str | None = Query(
        default=None, description="Filter by confidence level"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List detected subscriptions for the tenant."""
    svc = SubscriptionDetector(
        session_factory=db.session_factory,  # type: ignore[union-attr]
        tenant_id=auth.tenant_id,
    )

    subs = await svc.list_subscriptions(
        status=status_filter,
        confidence=confidence,
        limit=limit,
        offset=offset,
    )

    return {
        "items": [_sub_to_response(s).model_dump() for s in subs],
        "total": len(subs),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{subscription_id}", response_model=SubscriptionResponse)
async def get_subscription(
    subscription_id: str,
    auth: AuthContext = Depends(require_permission("subscriptions", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get a single detected subscription by ID."""
    from sqlalchemy import select

    from finance_sync.models.detected_subscription import (
        DetectedSubscription,
    )

    stmt = (
        select(DetectedSubscription).where(
            DetectedSubscription.id == subscription_id
        )  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()

    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_id!r} not found",
        )
    if sub.tenant_id != auth.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    return _sub_to_response(sub).model_dump()


@router.patch("/{subscription_id}", response_model=SubscriptionResponse)
async def update_subscription(
    subscription_id: str,
    body: SubscriptionUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> dict[str, Any]:
    """Update a detected subscription (status, category, notes)."""
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    sub = await svc.update_subscription(
        subscription_id,
        status=body.status,
        category=body.category,
        user_notes=body.user_notes,
    )

    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_id!r} not found",
        )

    return _sub_to_response(sub).model_dump()
