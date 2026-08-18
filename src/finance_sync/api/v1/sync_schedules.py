"""Tenant-scoped sync-schedule API (Planning on the Sync Runs page).

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.

Endpoints
---------
* ``GET  /sync-schedules``          — list the tenant's schedules
  (``?scope=ingestion|export``, pagination)
* ``GET  /sync-schedules/{id}``     — read one schedule
* ``POST /sync-schedules/preview``  — preview next instants of a
  *proposed* (not-yet-saved) schedule; stateless, no row required
* ``GET  /sync-schedules/{id}/preview`` — server-computed next 3 instants
  of the *stored* schedule
* ``PATCH /sync-schedules/{id}``    — update schedule/timezone/enabled
  (requires ``sync:write``; optimistic ``version`` → HTTP 409 on stale)
* ``POST /sync-schedules/{id}/reset`` — restore the default schedule
* ``POST /sync-schedules/{id}/disable`` / ``/enable`` — quick toggle

All reads require ``sync:read``; writes require ``sync:write`` (the
existing configuration/sync-management permissions).  Object ids from
another tenant behave exactly like missing ids (uniform 404, no
existence leak).
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.models.sync_schedule import (
    DEFAULT_TIMEZONE,
    SCOPE_EXPORT,
    SCOPE_INGESTION,
)
from finance_sync.services.sync_schedule import (
    ScheduleConflictError,
    ScheduleNotFoundError,
    SyncScheduleService,
    describe_schedule,
)
from finance_sync.sync.schedule_spec import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    SUPPORTED_FREQUENCIES,
    ScheduleValidationError,
    human_readable,
    next_run_instants,
    validate_schedule,
    validate_timezone,
)

router = APIRouter(prefix="/sync-schedules", tags=["sync-schedules"])

_VALID_SCOPES = {SCOPE_INGESTION, SCOPE_EXPORT}


# ── Request / response models ─────────────────────────────────────────


class ScheduleUpdateRequest(BaseModel):
    """Partial update of a schedule.

    At least one of *schedule* / *timezone* / *enabled* must be present.
    *version* is the optimistic-lock value the caller based its edit on;
    a mismatch returns HTTP 409.
    """

    schedule: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Recurrence: {frequency: daily|weekdays|weekly|hourly, "
            "time: 'HH:MM', weekdays: [0-6], interval_hours: N}"
        ),
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone name (e.g. Europe/Amsterdam)",
    )
    enabled: bool | None = Field(
        default=None,
        description="False stops scheduled runs; manual runs stay possible",
    )
    version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optimistic-lock version from the last read; 409 when stale"
        ),
    )


class ScheduleResponse(BaseModel):
    """Public schedule representation — never credentials/payloads."""

    id: str
    scope: str
    target_id: str
    enabled: bool
    schedule: dict[str, Any]
    schema_version: int
    timezone: str
    version: int
    next_run_at: datetime | None
    last_scheduled_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_error: str | None
    human_readable: str
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class ScheduleListResponse(BaseModel):
    items: list[ScheduleResponse]
    total: int
    limit: int
    offset: int


class SchedulePreviewResponse(BaseModel):
    id: str
    next_runs: list[datetime]
    human_readable: str
    timezone: str


class ProposedSchedulePreviewRequest(BaseModel):
    """Preview the next occurrences of a *proposed* schedule.

    The UI editor sends the not-yet-saved values here so the live
    preview is computed by the same pure function the worker uses —
    never a client-side approximation.  No row is created or modified.
    """

    schedule: dict[str, Any] = Field(
        description=(
            "Recurrence: {frequency: daily|weekdays|weekly|hourly, "
            "time: 'HH:MM', weekdays: [0-6], interval_hours: N}"
        ),
    )
    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description="IANA timezone name (e.g. Europe/Amsterdam)",
    )
    count: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of next instants to return (default 3)",
    )


# ── Serialisation helpers ─────────────────────────────────────────────


def _to_response(row: Any) -> ScheduleResponse:
    return ScheduleResponse(
        id=str(row.id),
        scope=str(row.scope),
        target_id=str(row.target_id),
        enabled=bool(row.enabled),
        schedule=dict(row.schedule or {}),
        schema_version=int(row.schema_version or 1),
        timezone=str(row.timezone),
        version=int(row.version or 1),
        next_run_at=row.next_run_at,
        last_scheduled_at=row.last_scheduled_at,
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,
        last_run_error=row.last_run_error,
        human_readable=describe_schedule(row),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Schedule not found",
    )


def _validation_error(exc: ScheduleValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=str(exc),
    )


def _actor(auth: AuthContext) -> tuple[str | None, str | None, str | None]:
    """Return (actor_user_id, actor_role, actor_api_key_id)."""
    if auth.user is not None:
        return str(auth.user.id), str(auth.user.role), None
    if auth.api_key_result is not None and auth.api_key_result.api_key:
        return None, None, str(auth.api_key_result.api_key.id)
    return None, None, None


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(
    _auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Query(
        default=None,
        description="Filter by scope: 'ingestion' or 'export'",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ScheduleListResponse:
    """List the tenant's schedules (Planning rows)."""
    if scope is not None and scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"scope must be one of {sorted(_VALID_SCOPES)}",
        )
    svc = SyncScheduleService(db)
    rows = await svc.list_for_tenant(
        _auth.tenant_id,
        scope=scope,
        limit=limit,
        offset=offset,
    )
    return ScheduleListResponse(
        items=[_to_response(r) for r in rows],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/preview",
    response_model=SchedulePreviewResponse,
    status_code=status.HTTP_200_OK,
)
async def preview_proposed_schedule(
    body: ProposedSchedulePreviewRequest,
    _auth: AuthContext = Depends(require_permission("sync", "read")),
) -> SchedulePreviewResponse:
    """Preview the next instants of a *proposed* schedule.

    Stateless: validates the submitted recurrence + timezone with the
    same pure function the worker uses and returns the next *count*
    instants plus a human-readable summary.  Nothing is persisted and
    no row is required — the UI live-preview calls this before saving.

    ``id`` is empty because there is no stored schedule yet.
    """
    try:
        # Validate + normalise first so invalid input yields a clear
        # 422 (compute_next_run swallows errors and returns None).
        normalised = validate_schedule(body.schedule, timezone=body.timezone)
        validate_timezone(body.timezone)
        instants = next_run_instants(
            normalised,
            timezone=body.timezone,
            after=datetime.now(UTC),
            count=body.count,
        )
    except ScheduleValidationError as exc:
        raise _validation_error(exc) from None
    return SchedulePreviewResponse(
        id="",
        next_runs=instants,
        human_readable=human_readable(normalised, timezone=body.timezone),
        timezone=body.timezone,
    )


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: str,
    _auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Read one schedule (tenant-scoped)."""
    svc = SyncScheduleService(db)
    try:
        row = await svc.get_for_tenant(_auth.tenant_id, schedule_id)
    except ScheduleNotFoundError:
        raise _not_found() from None
    return _to_response(row)


@router.get("/{schedule_id}/preview", response_model=SchedulePreviewResponse)
async def preview_schedule(
    schedule_id: str,
    _auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
    count: int = Query(default=3, ge=1, le=10),
) -> SchedulePreviewResponse:
    """Server-computed next *count* run instants (matches the worker)."""
    svc = SyncScheduleService(db)
    try:
        row, instants = await svc.preview(
            _auth.tenant_id,
            schedule_id,
            count=count,
        )
    except ScheduleNotFoundError:
        raise _not_found() from None
    except ScheduleValidationError as exc:
        raise _validation_error(exc) from None
    return SchedulePreviewResponse(
        id=str(row.id),
        next_runs=instants,
        human_readable=describe_schedule(row),
        timezone=str(row.timezone),
    )


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdateRequest,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Update schedule fields; recompute ``next_run_at``; audit.

    A stale optimistic-lock *version* returns HTTP 409 and changes
    nothing.  Disabling stops future scheduled runs but leaves manual
    syncs/export possible.
    """
    if body.schedule is None and body.timezone is None and body.enabled is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=("Provide at least one of schedule, timezone or enabled"),
        )
    svc = SyncScheduleService(db)
    actor_user_id, actor_role, actor_api_key_id = _actor(auth)
    try:
        row = await svc.update(
            auth.tenant_id,
            schedule_id,
            schedule=body.schedule,
            timezone=body.timezone,
            enabled=body.enabled,
            version=body.version,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )
    except ScheduleNotFoundError:
        raise _not_found() from None
    except ScheduleConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from None
    except ScheduleValidationError as exc:
        raise _validation_error(exc) from None
    return _to_response(row)


@router.post("/{schedule_id}/reset", response_model=ScheduleResponse)
async def reset_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
    version: int | None = Query(default=None, ge=1),
) -> ScheduleResponse:
    """Restore the default schedule (weekdays 07:00, tenant tz)."""
    svc = SyncScheduleService(db)
    actor_user_id, actor_role, actor_api_key_id = _actor(auth)
    try:
        row = await svc.reset_to_default(
            auth.tenant_id,
            schedule_id,
            version=version,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )
    except ScheduleNotFoundError:
        raise _not_found() from None
    except ScheduleConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from None
    return _to_response(row)


@router.post("/{schedule_id}/disable", response_model=ScheduleResponse)
async def disable_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Disable a schedule: no new scheduled runs; manual runs stay possible."""
    svc = SyncScheduleService(db)
    actor_user_id, actor_role, actor_api_key_id = _actor(auth)
    try:
        row = await svc.update(
            auth.tenant_id,
            schedule_id,
            enabled=False,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )
    except ScheduleNotFoundError:
        raise _not_found() from None
    return _to_response(row)


@router.post("/{schedule_id}/enable", response_model=ScheduleResponse)
async def enable_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Enable a schedule; ``next_run_at`` is recomputed immediately."""
    svc = SyncScheduleService(db)
    actor_user_id, actor_role, actor_api_key_id = _actor(auth)
    try:
        row = await svc.update(
            auth.tenant_id,
            schedule_id,
            enabled=True,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_api_key_id=actor_api_key_id,
        )
    except ScheduleNotFoundError:
        raise _not_found() from None
    return _to_response(row)


# Re-export validation bounds for OpenAPI documentation.
__all__ = [
    "MAX_INTERVAL_HOURS",
    "MIN_INTERVAL_HOURS",
    "SUPPORTED_FREQUENCIES",
]
