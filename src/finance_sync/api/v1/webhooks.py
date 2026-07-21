"""Webhook CRUD endpoints — register, list, and delete webhook endpoints.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, HttpUrl

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.services.webhook import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── Request / Response DTOs ──────────────────────────────────────────


class CreateWebhookRequest(BaseModel):
    url: HttpUrl = Field(description="Webhook callback URL")
    events: list[str] = Field(
        min_length=1,
        description="Event types to subscribe to, e.g. ['sync.completed']",
    )
    description: str | None = Field(
        default=None, max_length=255, description="Optional label"
    )
    secret: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        description="HMAC signing secret (auto-generated if omitted)",
    )
    rate_limit_max_per_minute: int = Field(
        default=60, ge=1, le=600,
        description="Max deliveries per 60-second sliding window",
    )


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    description: str | None = None
    secret: str | None = None
    rate_limit_max_per_minute: int
    created_at: str | None = None
    updated_at: str | None = None


class WebhookListResponse(BaseModel):
    items: list[WebhookResponse]
    total: int


# ── Path helpers ──────────────────────────────────────────────────────


def _get_service(request: Request) -> WebhookService:
    """Get or create a WebhookService from the container."""
    from finance_sync.dependencies import get_container

    container = get_container(request)
    settings = container.settings

    # Cache the service on app state so the HTTP client is reused
    svc: WebhookService | None = getattr(
        request.app.state, "webhook_service_instance", None
    )
    if svc is None:
        svc = WebhookService(
            session_factory=container.session_factory,
            settings=settings,
        )
        request.app.state.webhook_service_instance = svc
    return svc


# ── POST /v1/webhooks ───────────────────────────────────────────────


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    request: Request,
    body: CreateWebhookRequest,
    auth: AuthContext = Depends(require_permission("webhooks", "write")),
) -> dict[str, Any]:
    """Register a new webhook endpoint."""
    svc = _get_service(request)
    wh = await svc.create_webhook(
        tenant_id=auth.tenant_id,
        url=str(body.url),
        events=body.events,
        secret=body.secret,
        description=body.description,
        rate_limit_max_per_minute=body.rate_limit_max_per_minute,
    )
    return WebhookResponse(
        id=str(wh.id),
        url=wh.url,
        events=wh.events or [],
        is_active=wh.is_active,
        description=wh.description,
        secret=wh.secret,
        rate_limit_max_per_minute=wh.rate_limit_max_per_minute,
        created_at=wh.created_at.isoformat() if wh.created_at else None,
        updated_at=wh.updated_at.isoformat() if wh.updated_at else None,
    ).model_dump()


# ── GET /v1/webhooks ─────────────────────────────────────────────────


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    request: Request,
    auth: AuthContext = Depends(require_permission("webhooks", "read")),
    event_type: str | None = Query(
        default=None,
        description="Filter by event type (e.g. 'sync.completed')",
    ),
) -> dict[str, Any]:
    """List registered webhooks for the authenticated tenant."""
    svc = _get_service(request)
    webhooks = await svc.list_webhooks(
        tenant_id=auth.tenant_id,
        event_type=event_type,
    )
    items = [
        WebhookResponse(
            id=str(wh.id),
            url=wh.url,
            events=wh.events or [],
            is_active=wh.is_active,
            description=wh.description,
            rate_limit_max_per_minute=wh.rate_limit_max_per_minute,
            created_at=wh.created_at.isoformat() if wh.created_at else None,
            updated_at=wh.updated_at.isoformat() if wh.updated_at else None,
        )
        for wh in webhooks
    ]
    return {"items": [i.model_dump() for i in items], "total": len(items)}


# ── GET /v1/webhooks/{id} ───────────────────────────────────────────


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    request: Request,
    webhook_id: str,
    auth: AuthContext = Depends(require_permission("webhooks", "read")),
) -> dict[str, Any]:
    """Get a single webhook by ID."""
    svc = _get_service(request)
    wh = await svc.get_webhook(webhook_id, tenant_id=auth.tenant_id)
    if wh is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )
    return WebhookResponse(
        id=str(wh.id),
        url=wh.url,
        events=wh.events or [],
        is_active=wh.is_active,
        description=wh.description,
        rate_limit_max_per_minute=wh.rate_limit_max_per_minute,
        created_at=wh.created_at.isoformat() if wh.created_at else None,
        updated_at=wh.updated_at.isoformat() if wh.updated_at else None,
    ).model_dump()


# ── DELETE /v1/webhooks/{id} ─────────────────────────────────────────


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    request: Request,
    webhook_id: str,
    auth: AuthContext = Depends(require_permission("webhooks", "delete")),
) -> None:
    """Delete a webhook endpoint."""
    svc = _get_service(request)
    deleted = await svc.delete_webhook(webhook_id, tenant_id=auth.tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )
