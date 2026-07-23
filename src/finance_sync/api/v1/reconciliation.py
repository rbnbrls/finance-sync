"""Reconciliation API endpoints — trigger runs and view findings.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal  # noqa: TC003
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_container, get_db
from finance_sync.services.reconciliation import ReconciliationService

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


# ── Request / Response DTOs ───────────────────────────────────────────


class ReconciliationTriggerRequest(BaseModel):
    """Request body to trigger a new reconciliation run."""

    account_ids: list[str] | None = Field(
        default=None,
        description="Optional subset of account IDs to analyze",
    )
    date_from: datetime | None = Field(
        default=None,
        description="Earliest transaction date (default 90 days ago)",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Latest transaction date (default now)",
    )
    threshold_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        description="Max hour gap for duplicate candidates",
    )


class ReconciliationSummary(BaseModel):
    """Summary stats from a reconciliation run."""

    by_kind: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)


class ReconciliationRunResponse(BaseModel):
    """Public representation of a reconciliation run."""

    id: str
    tenant_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    scope: dict[str, Any] | None = None
    finding_count: int | None = None
    summary: ReconciliationSummary | None = None
    error_message: str | None = None
    created_at: datetime | None = None


class ReconciliationResultResponse(BaseModel):
    """Public representation of a single reconciliation finding."""

    id: str
    run_id: str
    kind: str
    severity: str
    account_id: str | None = None
    provider_key: str | None = None
    other_provider_key: str | None = None
    transaction_id_a: str | None = None
    transaction_id_b: str | None = None
    external_transaction_id_a: str | None = None
    external_transaction_id_b: str | None = None
    amount: Decimal | None = None
    other_amount: Decimal | None = None
    occurred_at: datetime | None = None
    description: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime | None = None


class ReconciliationRunListResponse(BaseModel):
    """List of reconciliation runs."""

    items: list[ReconciliationRunResponse]
    total: int
    limit: int
    offset: int


class ReconciliationRunDetailResponse(BaseModel):
    """A reconciliation run with its findings."""

    run: ReconciliationRunResponse
    results: list[ReconciliationResultResponse]
    total_results: int
    result_limit: int
    result_offset: int


# ── Helpers ───────────────────────────────────────────────────────────


def _run_to_response(run: object) -> ReconciliationRunResponse:
    """Convert an ORM ReconciliationRun to its response DTO."""
    s = getattr(run, "summary", None) or {}
    summary = ReconciliationSummary(
        by_kind=s.get("by_kind", {}) if isinstance(s, dict) else {},
        by_severity=s.get("by_severity", {}) if isinstance(s, dict) else {},
    )
    return ReconciliationRunResponse(
        id=str(getattr(run, "id", "")),
        tenant_id=str(getattr(run, "tenant_id", "")),
        status=str(getattr(run, "status", "")),
        started_at=getattr(run, "started_at", datetime.now(UTC)),
        completed_at=getattr(run, "completed_at", None),
        scope=getattr(run, "scope", None),
        finding_count=getattr(run, "finding_count", None),
        summary=summary,
        error_message=getattr(run, "error_message", None),
        created_at=getattr(run, "created_at", None),
    )


def _result_to_response(result: object) -> ReconciliationResultResponse:
    """Convert an ORM ReconciliationResult to its response DTO."""
    return ReconciliationResultResponse(
        id=str(getattr(result, "id", "")),
        run_id=str(getattr(result, "run_id", "")),
        kind=str(getattr(result, "kind", "")),
        severity=str(getattr(result, "severity", "")),
        account_id=str(getattr(result, "account_id", ""))
        if getattr(result, "account_id", None)
        else None,
        provider_key=getattr(result, "provider_key", None),
        other_provider_key=getattr(result, "other_provider_key", None),
        transaction_id_a=str(getattr(result, "transaction_id_a", ""))
        if getattr(result, "transaction_id_a", None)
        else None,
        transaction_id_b=str(getattr(result, "transaction_id_b", ""))
        if getattr(result, "transaction_id_b", None)
        else None,
        external_transaction_id_a=getattr(
            result, "external_transaction_id_a", None
        ),
        external_transaction_id_b=getattr(
            result, "external_transaction_id_b", None
        ),
        amount=getattr(result, "amount", None),
        other_amount=getattr(result, "other_amount", None),
        occurred_at=getattr(result, "occurred_at", None),
        description=getattr(result, "description", None),
        details=getattr(result, "details", None),
        created_at=getattr(result, "created_at", None),
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_reconciliation(
    body: ReconciliationTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
) -> dict[str, Any]:
    """Trigger a new reconciliation run.

    The run executes synchronously and returns the completed run with
    finding count and summary stats.
    """
    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    run = await svc.reconcile(
        account_ids=body.account_ids,
        date_from=body.date_from,
        date_to=body.date_to,
        threshold_hours=body.threshold_hours,
    )

    return _run_to_response(run).model_dump()


@router.get("", response_model=ReconciliationRunListResponse)
async def list_reconciliation_runs(
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List reconciliation runs for the tenant."""
    svc = ReconciliationService(
        session_factory=db.session_factory,  # type: ignore[union-attr]
        tenant_id=auth.tenant_id,
    )

    runs = await svc.list_runs(limit=limit, offset=offset)
    return {
        "items": [_run_to_response(r).model_dump() for r in runs],
        "total": len(runs),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{run_id}", response_model=ReconciliationRunDetailResponse)
async def get_reconciliation_run(
    run_id: str,
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    db: AsyncSession = Depends(get_db),
    result_limit: int = Query(default=100, ge=1, le=500),
    result_offset: int = Query(default=0, ge=0),
    kind: str | None = Query(default=None),
    severity: str | None = Query(default=None),
) -> dict[str, Any]:
    """Get a reconciliation run with its findings."""
    svc = ReconciliationService(
        session_factory=db.session_factory,  # type: ignore[union-attr]
        tenant_id=auth.tenant_id,
    )

    run, results, total = await svc.get_run_with_results(
        run_id,
        result_limit=result_limit,
        result_offset=result_offset,
        kind_filter=kind,
        severity_filter=severity,
    )

    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reconciliation run {run_id!r} not found",
        )

    if run.tenant_id != auth.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    return {
        "run": _run_to_response(run).model_dump(),
        "results": [_result_to_response(r).model_dump() for r in results],
        "total_results": total,
        "result_limit": result_limit,
        "result_offset": result_offset,
    }
