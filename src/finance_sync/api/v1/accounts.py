"""Read-only account endpoints — list accounts, transactions, and balances.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.services.read_api import (
    AccountDetailResponse,
    AccountSummary,
    BalanceListResponse,
    ReadService,
    TransactionListResponse,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ── Path helpers ──────────────────────────────────────────────────────


def _get_service(session: AsyncSession) -> ReadService:
    return ReadService(session)


# ── GET /v1/accounts ─────────────────────────────────────────────────


@router.get("", response_model=AccountDetailResponse)
async def list_accounts(
    request: Request,  # noqa: ARG001
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
    account_type: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> dict[str, Any]:
    """List all accounts for the authenticated tenant with optional filters."""
    svc = _get_service(db)
    result = await svc.list_accounts(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        account_type=account_type,
        is_active=is_active,
    )
    return result.model_dump()


# ── GET /v1/accounts/{id} ────────────────────────────────────────────


@router.get("/{account_id}", response_model=AccountSummary)
async def get_account(
    account_id: str,
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get a single account by ID."""
    svc = _get_service(db)
    account = await svc.get_account(
        tenant_id=auth.tenant_id, account_id=account_id
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return account.model_dump()


# ── GET /v1/accounts/{id}/transactions ───────────────────────────────


@router.get(
    "/{account_id}/transactions",
    response_model=TransactionListResponse,
)
async def list_account_transactions(
    account_id: str,
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="occurred_at"),
    sort_order: str = Query(default="desc"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    transaction_type: str | None = Query(default=None),
    security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """List transactions for an account with optional date range, type, and
    security filters."""
    svc = _get_service(db)
    result = await svc.list_account_transactions(
        tenant_id=auth.tenant_id,
        account_id=account_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        date_from=date_from,
        date_to=date_to,
        transaction_type=transaction_type,
        security_id=security_id,
    )
    return result.model_dump()


# ── GET /v1/accounts/{id}/balances ───────────────────────────────────


@router.get("/{account_id}/balances", response_model=BalanceListResponse)
async def list_account_balances(
    account_id: str,
    auth: AuthContext = Depends(require_permission("balances", "read")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    balance_kind: str | None = Query(default=None),
) -> dict[str, Any]:
    """List balance snapshots (time series) for an account."""
    svc = _get_service(db)
    result = await svc.list_account_balances(
        tenant_id=auth.tenant_id,
        account_id=account_id,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
        balance_kind=balance_kind,
    )
    return result.model_dump()
