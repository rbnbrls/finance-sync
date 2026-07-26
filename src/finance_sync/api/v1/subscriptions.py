"""Subscription detection API endpoints — run detection and manage results.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.services.subscription_detector import SubscriptionDetector
from finance_sync.services.subscription_detector.service import (
    Subscription as SubscriptionResult,
)
from finance_sync.services.subscription_detector.service import (
    SubscriptionDetectionService,
)

logger = structlog.get_logger("finance_sync.api.v1.subscriptions")

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


class AnalysisTriggerRequest(BaseModel):
    """Request body to trigger dry-run subscription analysis."""

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
    use_merchant_classifier: bool = Field(
        default=True,
        description=(
            "Whether to enrich with merchant sector/classification data"
        ),
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
    fundamentals_available: bool = False
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
        description="New status: active, paused, cancelled, ignored",
    )
    category: str | None = Field(
        default=None,
        description="Override subscription category",
    )
    user_notes: str | None = Field(
        default=None,
        description="User notes or label",
    )


class DetectionResultItem(BaseModel):
    """A detected subscription from the integrated detection service.

    Unlike :class:`SubscriptionResponse`, this model does not include
    database-specific fields (id, tenant_id, etc.) because the detection
    result is ephemeral — it has not yet been persisted.
    """

    merchant_name: str = Field(..., description="Normalised merchant name")
    raw_description: str | None = Field(
        default=None, description="Most recent raw transaction description"
    )
    amount: Decimal = Field(
        ..., description="Typical subscription amount (positive)"
    )
    currency_code: str = Field(
        default="EUR", description="ISO-4217 currency code"
    )
    frequency_days: int | None = Field(
        default=None, description="Expected interval in days"
    )
    frequency_label: str | None = Field(
        default=None, description="Human-readable frequency label"
    )
    confidence: str = Field(
        ..., description="Confidence level (high/medium/low)"
    )
    detection_score: float = Field(
        default=0.0, description="Numeric detection score (0.0-1.0)"
    )
    detection_method: str = Field(..., description="Detection strategy used")
    status: str = Field(..., description="Subscription status")
    transaction_ids: list[str] = Field(
        default=[], description="IDs of matched transactions"
    )
    account_id: str = Field(
        default="", description="Primary account identifier"
    )
    provider_key: str = Field(default="", description="Connector provider key")
    security_id: str | None = Field(
        default=None, description="DB security identifier"
    )
    fundamentals_available: bool = Field(
        default=False,
        description="Whether fundamentals data (PE ratio, dividend yield) was used in classification",
    )
    sector: str | None = Field(default=None, description="GICS sector")
    category: str | None = Field(
        default=None, description="Subscription category"
    )
    first_detected_at: datetime | None = Field(
        default=None, description="Earliest matched transaction date"
    )
    last_detected_at: datetime | None = Field(
        default=None, description="Most recent matched transaction date"
    )
    occurrence_count: int = Field(
        default=0, description="Number of matched transactions"
    )
    details: dict[str, Any] = Field(
        default_factory=dict, description="Extra diagnostic context"
    )


class ConfirmSubscriptionRequest(BaseModel):
    """Request body to confirm a detected subscription."""

    user_notes: str | None = Field(
        default=None,
        description="Optional confirmation notes",
    )


class IgnoreSubscriptionRequest(BaseModel):
    """Request body to ignore a detected subscription."""

    reason: str | None = Field(
        default=None,
        description="Reason for ignoring this subscription",
    )


class DeleteSubscriptionResponse(BaseModel):
    """Response after deleting a subscription."""

    deleted: bool = Field(..., description="Whether the record was deleted")


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
        fundamentals_available=bool(
            getattr(sub, "fundamentals_available", False)
        ),
        first_detected_at=getattr(sub, "first_detected_at", datetime.now(UTC)),
        last_detected_at=getattr(sub, "last_detected_at", datetime.now(UTC)),
        occurrence_count=getattr(sub, "occurrence_count", 0),
        detection_score=getattr(sub, "detection_score", None),
        details=getattr(sub, "details", None),
        user_notes=getattr(sub, "user_notes", None),
        created_at=getattr(sub, "created_at", None),
    )


def _sub_from_detection(sub: SubscriptionResult) -> DetectionResultItem:
    """Convert a ``Subscription`` detection dataclass to the API response model.

    Handles enum-to-string and Decimal serialisation so the response is
    JSON-safe.
    """
    return DetectionResultItem(
        merchant_name=sub.merchant_name,
        raw_description=sub.raw_description,
        amount=sub.amount,
        currency_code=sub.currency_code,
        frequency_days=sub.frequency_days,
        frequency_label=sub.frequency_label,
        confidence=(
            sub.confidence.value
            if hasattr(sub.confidence, "value")
            else str(sub.confidence)
        ),
        detection_score=sub.detection_score,
        detection_method=(
            sub.detection_method.value
            if hasattr(sub.detection_method, "value")
            else str(sub.detection_method)
        ),
        status=(
            sub.status.value
            if hasattr(sub.status, "value")
            else str(sub.status)
        ),
        transaction_ids=sub.transaction_ids,
        account_id=sub.account_id,
        provider_key=sub.provider_key,
        security_id=sub.security_id,
        fundamentals_available=sub.fundamentals_available,
        sector=sub.sector,
        category=sub.category,
        first_detected_at=sub.first_detected_at,
        last_detected_at=sub.last_detected_at,
        occurrence_count=sub.occurrence_count,
        details=sub.details,
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post(
    "/detect",
    response_model=list[DetectionResultItem],
    status_code=status.HTTP_200_OK,
)
async def detect_subscriptions(
    body: DetectionTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> list[DetectionResultItem]:
    """Run integrated subscription detection on transaction history.

    Uses the combined merchant-classification and pattern-recognition
    service (``SubscriptionDetectionService``) to analyse outgoing
    transactions and return newly detected subscriptions with cross-
    validated confidence scores.

    Response items do **not** include database IDs — the results are
    ephemeral detection outputs.  Use ``POST /{id}/confirm`` on an
    existing subscription to persist it.
    """
    container = get_container(request)
    svc = SubscriptionDetectionService(
        session_factory=container.session_factory,
        min_occurrences=body.min_occurrences,
    )

    try:
        subscriptions = await svc.detect_subscriptions(
            user_id=auth.tenant_id,
            date_from=body.date_from,
            date_to=body.date_to,
        )
    except ValueError as exc:
        logger.error(
            "detection_configuration_error",
            error=str(exc),
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "detection_failed",
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Subscription detection failed",
        ) from exc

    return [_sub_from_detection(s) for s in subscriptions]


@router.post(
    "/analyze",
    response_model=list[DetectionResultItem],
    status_code=status.HTTP_200_OK,
)
async def analyze_subscriptions(
    body: AnalysisTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> list[DetectionResultItem]:
    """Dry-run subscription detection without persisting results.

    Runs the full integrated detection pipeline — merchant classification,
    pattern recognition, and cross-validation — but returns ephemeral
    results instead of saving them to the database.  Useful for previewing
    what would be detected.

    The ``use_merchant_classifier`` parameter is accepted for backward
    compatibility; the integrated service always applies merchant
    classification when available.
    """
    container = get_container(request)
    svc = SubscriptionDetectionService(
        session_factory=container.session_factory,
        min_occurrences=body.min_occurrences,
    )

    try:
        subscriptions = await svc.detect_subscriptions(
            user_id=auth.tenant_id,
            date_from=body.date_from,
            date_to=body.date_to,
        )
    except ValueError as exc:
        logger.error(
            "detection_configuration_error",
            error=str(exc),
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "detection_failed",
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Subscription detection failed",
        ) from exc

    return [_sub_from_detection(s) for s in subscriptions]


@router.get(
    "/detected",
    response_model=list[DetectionResultItem],
    status_code=status.HTTP_200_OK,
)
async def get_detected_subscriptions(
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "read")),
    date_from: datetime | None = Query(
        default=None,
        description="Earliest transaction date (default 365 days ago)",
    ),
    date_to: datetime | None = Query(
        default=None,
        description="Latest transaction date (default now)",
    ),
    min_occurrences: int = Query(
        default=2,
        ge=2,
        le=24,
        description="Minimum occurrences to consider a pattern",
    ),
) -> list[DetectionResultItem]:
    """Run subscription detection on transaction history and return results.

    A read-only variant of POST /detect that returns ephemeral detection
    results without persisting them.  Uses the combined merchant-classification
    and pattern-recognition service (SubscriptionDetectionService) to analyse
    outgoing transactions and return currently detected subscriptions.

    Response items do **not** include database IDs — the results are
    ephemeral detection outputs.  Use ``POST /{id}/confirm`` on an
    existing subscription to persist it.
    """
    container = get_container(request)
    svc = SubscriptionDetectionService(
        session_factory=container.session_factory,
        min_occurrences=min_occurrences,
    )

    try:
        subscriptions = await svc.detect_subscriptions(
            user_id=auth.tenant_id,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        logger.error(
            "detection_configuration_error",
            error=str(exc),
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "detection_failed",
            error=str(exc),
            user_id=auth.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Subscription detection failed",
        ) from exc

    return [_sub_from_detection(s) for s in subscriptions]


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "read")),
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
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
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


@router.post(
    "/{subscription_id}/confirm",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
)
async def confirm_subscription(
    subscription_id: str,
    body: ConfirmSubscriptionRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> dict[str, Any]:
    """Confirm a detected subscription as legitimate.

    Sets the subscription status to ``active`` and optionally records
    confirmation notes.  Use this when a user verifies the detected
    pattern is a real subscription they want to track.
    """
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    sub = await svc.confirm_subscription(
        subscription_id,
        user_notes=body.user_notes,
    )

    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_id!r} not found",
        )

    return _sub_to_response(sub).model_dump()


@router.post(
    "/{subscription_id}/ignore",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
)
async def ignore_subscription(
    subscription_id: str,
    body: IgnoreSubscriptionRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> dict[str, Any]:
    """Ignore a detected subscription.

    Sets the subscription status to ``ignored`` and optionally records
    an ignore reason.  Use this when a user dismisses a false positive
    or decides not to track a particular subscription.
    """
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    sub = await svc.ignore_subscription(
        subscription_id,
        reason=body.reason,
    )

    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_id!r} not found",
        )

    return _sub_to_response(sub).model_dump()


@router.delete(
    "/{subscription_id}",
    response_model=DeleteSubscriptionResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_subscription(
    subscription_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("subscriptions", "write")),
) -> dict[str, Any]:
    """Permanently delete a detected subscription record.

    Removes the subscription from the database entirely.  Unlike
    ignoring (which marks the status), this cannot be undone.
    """
    container = get_container(request)
    svc = SubscriptionDetector(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    deleted = await svc.delete_subscription(subscription_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_id!r} not found",
        )

    return {"deleted": True}
