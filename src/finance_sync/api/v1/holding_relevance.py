"""Holding-relevance REST API.

Implements backlog/plus-relevant-nieuws-en-events.md read/write contract:

* ``GET /holding-relevance/feed`` — ranked, clustered holding feed with
  filters on security, account, item type, date and unread/acknowledged
  status.  Every cluster always carries source URLs, published/fetched
  timestamps and a freshness value.
* ``GET /holding-relevance/calendar`` — upcoming/past event clusters.
* ``POST /holding-relevance/clusters/{id}/ack`` — per-user ack (or
  un-ack) of one cluster.
* ``POST /holding-relevance/corrections`` — per-user false-positive
  correction (suppression only; never deletes the observation).
* ``GET/PUT /holding-relevance/notifications/preferences`` — opt-in
  notification settings (lockscreen-safe by default).
* ``POST /holding-relevance/notifications/{cluster_id}/send`` —
  fire one deduplicated, lockscreen-safe notification payload.

All endpoints are tenant-scoped via the existing auth dependency.  User
identity for ack/correction semantics comes from ``auth.principal_id``
(the signed-in user); without a user principal, acks/corrections are a
no-op-safe 200 with an explanatory detail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container
from finance_sync.services.holding_relevance import HoldingRelevanceService

router = APIRouter(
    prefix="/holding-relevance",
    tags=["holding-relevance"],
)


def _service(request: Request) -> HoldingRelevanceService:
    """Create a service bound to a fresh session from the container."""
    container = get_container(request)
    from finance_sync.db.uow import UnitOfWork

    session = container.session_factory()
    return HoldingRelevanceService(UnitOfWork(session))


# ── Feed ───────────────────────────────────────────────────────────────


@router.get("/feed")
async def holding_relevance_feed(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
    security_id: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    item_type: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    unread_only: bool = Query(default=False),
    acknowledged: bool | None = Query(default=None),
    include_stale: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Return the ranked, clustered holding feed for the tenant.

    Filters are applied as data (parameterised) — a malicious value like
    ``AAPL' OR '1'='1`` matches no rows instead of executing SQL.  A
    cross-tenant ``security_id`` / ``account_id`` returns an empty feed,
    never an error and never foreign rows.
    """
    svc = _service(request)
    try:
        return await svc.feed(
            auth.tenant_id,
            user_id=auth.principal_id,
            security_id=security_id,
            account_id=account_id,
            item_type=item_type,
            date_from=date_from,
            date_to=date_to,
            unread_only=unread_only,
            acknowledged=acknowledged,
            include_stale=include_stale,
            limit=limit,
            offset=offset,
        )
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


# ── Calendar ───────────────────────────────────────────────────────────


@router.get("/calendar")
async def holding_relevance_calendar(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Return upcoming/past event clusters for the calendar view."""
    svc = _service(request)
    try:
        return await svc.calendar(
            auth.tenant_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


# ── Acknowledgement ────────────────────────────────────────────────────


class AckRequest(BaseModel):
    """Body for ack/un-ack of one cluster."""

    acknowledged: bool = Field(
        default=True,
        description="True to mark the cluster acknowledged, False to un-ack",
    )


@router.post("/clusters/{cluster_id}/ack")
async def acknowledge_cluster(
    request: Request,
    cluster_id: str,
    body: AckRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> dict[str, Any]:
    """Set (or clear) the per-user acknowledgement for *cluster_id*.

    Idempotent.  A cross-tenant cluster id returns 404 (never leaks
    existence).  Adding a source link to a cluster later never resets
    the user's ack.
    """
    svc = _service(request)
    try:
        ok = await svc.set_ack(
            auth.tenant_id,
            auth.principal_id,
            cluster_id,
            body.acknowledged,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cluster not found",
            )
        await svc._uow.commit()  # type: ignore[reportPrivateUsage]
        return {"cluster_id": cluster_id, "acknowledged": body.acknowledged}
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


# ── Corrections ────────────────────────────────────────────────────────


class CorrectionRequest(BaseModel):
    """Body for a false-positive correction."""

    item_id: str = Field(
        description="The observation that was a false positive"
    )
    security_id: str | None = Field(
        default=None,
        description=(
            "Security the user corrected; omit when dismissing generically"
        ),
    )
    action: str = Field(
        default="dismiss",
        description="dismiss or reassign",
    )
    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Free-form user note (sanitised before persistence)",
    )


@router.post("/corrections")
async def create_correction(
    request: Request,
    body: CorrectionRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> dict[str, Any]:
    """File a per-user false-positive correction for one observation.

    Suppresses the match in the correcting user's feed and records
    feedback for the future matcher.  Never deletes the underlying
    observation and never affects other tenants.  A cross-tenant item
    id returns 404.
    """
    svc = _service(request)
    try:
        ok = await svc.correct(
            auth.tenant_id,
            auth.principal_id,
            body.item_id,
            security_id=body.security_id,
            action=body.action,
            reason=body.reason,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Observation not found",
            )
        await svc._uow.commit()  # type: ignore[reportPrivateUsage]
        return {"item_id": body.item_id, "status": "corrected"}
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


# ── Notification preferences ───────────────────────────────────────────


@router.get("/notifications/preferences")
async def get_notification_preferences(
    request: Request,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "read")
    ),
) -> dict[str, Any]:
    """Return the user's opt-in notification settings."""
    svc = _service(request)
    try:
        return await svc.get_notification_preference(
            auth.tenant_id, auth.principal_id
        )
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


class NotificationPreferenceRequest(BaseModel):
    """Body to create/update notification preferences."""

    enabled: bool | None = Field(
        default=None,
        description="Opt-in master switch (off by default)",
    )
    lockscreen_safe: bool | None = Field(
        default=None,
        description=(
            "When True (default) the payload never leaks position sizes "
            "or financial values on the lockscreen"
        ),
    )
    event_types: list[str] | None = Field(
        default=None,
        description="Allowed event types; NULL/empty = all",
    )


@router.put("/notifications/preferences")
async def update_notification_preferences(
    request: Request,
    body: NotificationPreferenceRequest,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> dict[str, Any]:
    """Create/update the user's opt-in notification settings."""
    svc = _service(request)
    try:
        result = await svc.set_notification_preference(
            auth.tenant_id,
            auth.principal_id,
            enabled=body.enabled,
            lockscreen_safe=body.lockscreen_safe,
            event_types=body.event_types,
        )
        await svc._uow.commit()  # type: ignore[reportPrivateUsage]
        return result
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]


@router.post("/notifications/{cluster_id}/send")
async def send_cluster_notification(
    request: Request,
    cluster_id: str,
    auth: AuthContext = Depends(
        require_permission("market-intelligence", "write")
    ),
) -> dict[str, Any]:
    """Fire one deduplicated, lockscreen-safe notification for a cluster.

    Only fires when the user opted in; dedupes per
    (user, cluster, event_type).  The payload never carries position
    sizes or financial values unless the user explicitly disabled the
    lockscreen-safe flag.
    """
    svc = _service(request)
    try:
        result = await svc.notify_eligible(
            auth.tenant_id, auth.principal_id, cluster_id
        )
        if result.get("sent"):
            await svc._uow.commit()  # type: ignore[reportPrivateUsage]
        return result
    finally:
        await svc._uow.session.aclose()  # type: ignore[reportPrivateUsage]
