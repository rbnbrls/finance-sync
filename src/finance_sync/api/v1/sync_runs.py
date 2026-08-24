"""Read-only sync-run history endpoint.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

import json
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import (
    CONNECTION_STATUS_PAUSED,
    Credential,
)
from finance_sync.models.sync_run import SyncRun
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.services.auth import decrypt_credential
from finance_sync.services.read_api import ReadService, SyncRunListResponse
from finance_sync.services.retry_lock import retry_lease
from finance_sync.sync.orchestrator import SyncOrchestrator

router = APIRouter(prefix="/sync-runs", tags=["sync-runs"])


class SyncRunDetailResponse(BaseModel):
    id: str
    connector: str
    connection_id: str | None
    status: str
    started_at: Any
    completed_at: Any
    duration_seconds: float | None
    items_processed: int | None
    warnings: list[str]
    unresolved_securities: int
    cursor: Any
    error_message: str | None
    error_category: str | None


class SyncRetryResponse(BaseModel):
    run_id: str | None
    status: str
    link: str | None
    error_message: str | None


def _detail(row: SyncRun, unresolved: int) -> SyncRunDetailResponse:
    completed = row.completed_at
    duration = (
        (completed - row.started_at).total_seconds() if completed else None
    )
    return SyncRunDetailResponse(
        id=str(row.id),
        connector=row.connector,
        connection_id=str(row.connection_id) if row.connection_id else None,
        status=str(row.status),
        started_at=row.started_at,
        completed_at=completed,
        duration_seconds=duration,
        items_processed=row.items_processed,
        warnings=list(row.warnings or []),
        unresolved_securities=unresolved,
        cursor=row.cursor,
        error_category=row.error_category,
        error_message=row.error_message,
    )


async def _tenant_run(
    db: AsyncSession, tenant_id: str, run_id: str
) -> tuple[SyncRun, Credential]:
    result = await db.execute(
        select(SyncRun, Credential)
        .join(Credential, Credential.id == SyncRun.connection_id)
        .where(SyncRun.id == run_id, Credential.tenant_id == tenant_id)
    )
    pair = result.one_or_none()
    if pair is None:
        raise HTTPException(status_code=404, detail="Sync run not found")
    return pair[0], pair[1]


async def _unresolved_count(
    db: AsyncSession, tenant_id: str, connector: str
) -> int:
    """Count unresolved records within the authenticated tenant."""
    value = await db.scalar(
        select(func.count(UnresolvedSecurity.id)).where(
            UnresolvedSecurity.tenant_id == tenant_id,
            UnresolvedSecurity.provider_key == connector,
            UnresolvedSecurity.resolved_security_id.is_(None),
        )
    )
    return int(value or 0)


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=SyncRunListResponse)
async def list_sync_runs(
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    connector: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort_by: str = Query(default="started_at"),
    sort_order: str = Query(default="desc"),
) -> dict[str, Any]:
    """List sync run history with status counts per connector."""
    svc = _get_service(db)
    result = await svc.list_sync_runs(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        connector=connector,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return result.model_dump()


@router.get("/{run_id}", response_model=SyncRunDetailResponse)
async def get_sync_run(
    run_id: str,
    auth: AuthContext = Depends(require_permission("sync", "read")),
    db: AsyncSession = Depends(get_db),
) -> SyncRunDetailResponse:
    """Return a tenant-scoped, sanitized operational run detail."""
    run, _ = await _tenant_run(db, auth.tenant_id, run_id)
    return _detail(
        run,
        await _unresolved_count(db, auth.tenant_id, run.connector),
    )


@router.post(
    "/{run_id}/retry",
    response_model=SyncRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_sync_run(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("sync", "write")),
    db: AsyncSession = Depends(get_db),
) -> SyncRetryResponse:
    """Retry one failed connection-scoped run through the normal orchestrator."""
    run, credential = await _tenant_run(db, auth.tenant_id, run_id)
    if str(run.status) != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed sync runs can be retried (run is {run.status!r})",
        )
    if credential.status == CONNECTION_STATUS_PAUSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Paused connections cannot be retried",
        )
    container = get_container(request)
    if container.settings.redis_url is not None:
        async with retry_lease(
            container.redis_client,
            tenant_id=auth.tenant_id,
            kind="sync",
            item_id=run_id,
        ) as lease:
            if not lease.acquired:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A retry for this sync run is already in progress",
                )
            return await _retry_sync_run_locked(
                run_id, auth, db, request, run, credential, container
            )
    return await _retry_sync_run_locked(
        run_id, auth, db, request, run, credential, container
    )


async def _retry_sync_run_locked(
    run_id: str,
    auth: AuthContext,
    db: AsyncSession,
    request: Request,
    run: SyncRun,
    credential: Credential,
    container: Any,
) -> SyncRetryResponse:
    """Execute a retry while the caller owns the Redis lease."""
    raw = decrypt_credential(
        credential.encrypted_payload, credential.nonce, container.settings
    )
    try:
        credentials: dict[str, str] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        credentials = {"api_key": raw}
    options: dict[str, Any] = {}
    try:
        stored = json.loads(credential.description or "{}")
        if isinstance(stored, dict):
            options = {
                k: v
                for k, v in cast(dict[str, Any], stored).items()
                if k != "_label"
            }
    except (TypeError, json.JSONDecodeError):
        pass
    result = await SyncOrchestrator(
        session_factory=container.session_factory,
        registry=ConnectorRegistry(),
        tenant_id=auth.tenant_id,
        settings=container.settings,
    ).run_sync(
        provider_type=credential.provider_key,
        config=ConnectorConfig(
            provider_type=credential.provider_key,
            credentials=credentials,
            options=options,
            connection_id=str(credential.id),
            selected_accounts=list(credential.selected_accounts or []),
        ),
        connection_id=str(credential.id),
        selected_accounts=list(credential.selected_accounts or []),
    )
    latest = await db.execute(
        select(SyncRun.id)
        .where(SyncRun.connection_id == str(credential.id))
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    new_id = latest.scalar_one_or_none()
    return SyncRetryResponse(
        run_id=str(new_id) if new_id else None,
        status=str(result.status),
        link=f"/api/v1/sync-runs/{new_id}" if new_id else None,
        error_message=result.error_message,
    )
