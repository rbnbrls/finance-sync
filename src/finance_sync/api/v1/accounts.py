"""Read-only account endpoints — list accounts, transactions, and balances.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_read_scope,
    require_permission,
)
from finance_sync.dependencies import get_db
from finance_sync.models.enums import TransactionType
from finance_sync.services.account_sharing import (
    AccountSharingError,
    claim_account,
    set_account_visibility,
    share_preview,
)
from finance_sync.services.read_api import (
    AccountDetailResponse,
    AccountSummary,
    BalanceListResponse,
    ReadService,
    TransactionListResponse,
)
from finance_sync.services.visibility import ReadScope

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ── Path helpers ──────────────────────────────────────────────────────


def _get_service(
    session: AsyncSession, scope: ReadScope | None = None
) -> ReadService:
    return ReadService(session, scope=scope)


# ── GET /v1/accounts ─────────────────────────────────────────────────


@router.get("", response_model=AccountDetailResponse)
async def list_accounts(
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
    scope: ReadScope = Depends(get_read_scope),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
    account_type: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> dict[str, Any]:
    """List all accounts for the authenticated tenant with optional filters."""
    svc = _get_service(db, scope=scope)
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
    scope: ReadScope = Depends(get_read_scope),
) -> dict[str, Any]:
    """Get a single account by ID."""
    svc = _get_service(db, scope=scope)
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
    scope: ReadScope = Depends(get_read_scope),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="occurred_at"),
    sort_order: str = Query(default="desc"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    transaction_type: TransactionType | None = Query(default=None),
    security_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """List transactions for an account with optional date range, type, and
    security filters."""
    svc = _get_service(db, scope=scope)
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
    scope: ReadScope = Depends(get_read_scope),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    balance_kind: str | None = Query(default=None),
) -> dict[str, Any]:
    """List balance snapshots (time series) for an account."""
    svc = _get_service(db, scope=scope)
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


# ── Household sharing ─────────────────────────────────────────────────


class SetVisibilityRequest(BaseModel):
    visibility: str = Field(
        ..., description="New visibility policy: 'private' or 'household'"
    )


class SharePreviewResponse(BaseModel):
    account_id: str
    account_name: str
    current_visibility: str
    target_visibility: str
    impact: dict[str, Any]


def _raise_sharing_error(exc: AccountSharingError) -> NoReturn:
    raise HTTPException(
        status_code=(
            status.HTTP_404_NOT_FOUND
            if exc.code == "not_found"
            else status.HTTP_403_FORBIDDEN
            if exc.code == "forbidden"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        ),
        detail=exc.message,
    )


@router.get("/{account_id}/share-preview", response_model=SharePreviewResponse)
async def get_share_preview(
    account_id: str,
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
) -> SharePreviewResponse:
    """Preview what would change in the household view when this account
    is shared (private → household) or made private (household →
    private): the number of transactions, holdings and balance snapshots
    that would appear or disappear, plus the current balance.

    The UI shows this before asking the owner to confirm.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        preview = await share_preview(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            actor=auth.user,
        )
    except AccountSharingError as exc:
        _raise_sharing_error(exc)
    return SharePreviewResponse(**preview)


@router.patch("/{account_id}/visibility", response_model=AccountSummary)
async def update_account_visibility(
    account_id: str,
    body: SetVisibilityRequest,
    auth: AuthContext = Depends(require_permission("accounts", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Share or unshare an account (owner-only).

    ``household`` makes the account visible to every household member and
    eligible for the shared Wealthfolio export; ``private`` restricts it
    to the owner (and tenant admins).  Every transition is audit-logged.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        account = await set_account_visibility(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            visibility=body.visibility,
            actor=auth.user,
        )
    except AccountSharingError as exc:
        _raise_sharing_error(exc)
    await db.commit()
    return {
        "id": str(account.id),
        "name": account.name,
        "account_type": account.account_type,
        "account_subtype": account.account_subtype,
        "currency_code": account.currency_code,
        "current_balance": account.current_balance,
        "available_balance": account.available_balance,
        "provider_key": account.provider_key,
        "is_active": account.is_active,
        "visibility": account.visibility,
        "owner_user_id": account.owner_user_id,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


@router.post("/{account_id}/claim", response_model=AccountSummary)
async def claim_unowned_account(
    account_id: str,
    auth: AuthContext = Depends(require_permission("accounts", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Claim a system-owned (legacy) account (admin only).

    Accounts migrated before household sharing have no owner; claiming
    assigns them to the acting admin so they can be shared or kept
    private under explicit ownership.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        account = await claim_account(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            actor=auth.user,
        )
    except AccountSharingError as exc:
        _raise_sharing_error(exc)
    await db.commit()
    return {
        "id": str(account.id),
        "name": account.name,
        "account_type": account.account_type,
        "account_subtype": account.account_subtype,
        "currency_code": account.currency_code,
        "current_balance": account.current_balance,
        "available_balance": account.available_balance,
        "provider_key": account.provider_key,
        "is_active": account.is_active,
        "visibility": account.visibility,
        "owner_user_id": account.owner_user_id,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }
