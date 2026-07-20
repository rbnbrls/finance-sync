"""Read-only sync-run history endpoint.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import ReadService, SyncRunListResponse

router = APIRouter(prefix="/sync-runs", tags=["sync-runs"])


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


@router.get("", response_model=SyncRunListResponse)
async def list_sync_runs(
    auth: AuthContext = Depends(require_permission("sync", "read")),  # noqa: ARG001
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
        limit=limit,
        offset=offset,
        connector=connector,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return result.model_dump()
