"""Read-only account endpoints — list accounts, transactions, and balances.

NOTE: ``from __future__ import annotations`` is intentionally omitted
because FastAPI needs runtime type introspection for OpenAPI generation.
"""

from datetime import datetime
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_read_scope,
    require_permission,
)
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.enums import TransactionType
from finance_sync.services.account_sharing import (
    AccountSharingError,
    claim_account,
    set_account_visibility,
    share_preview,
)
from finance_sync.services.export_cleanup import (
    ERR_CONFIRMATION_REQUIRED,
    delete_export_artifacts,
    describe_export_artifacts,
    quarantine_export_artifacts,
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


class ExportArtifactsResponse(BaseModel):
    """What was already exported for an account (read-only description)."""

    account_id: str
    account_name: str
    has_mapping: bool
    wf_account_name: str | None
    has_delivery_cursor: bool
    last_exported_at: str | None
    csv_file_count: int
    csv_files: list[str]
    quarantined_file_count: int


class ExportCleanupRequest(BaseModel):
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true to permanently delete already-exported data. "
            "False (default) rejects the request and touches nothing."
        ),
    )


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


def _export_output_dir(request: Request) -> Any:
    """Resolve the Wealthfolio exporter output dir from settings."""
    from pathlib import Path

    settings = get_container(request).settings
    return Path(
        getattr(settings, "wealthfolio_output_dir", "")
        or "/tmp/finance_sync_wealthfolio_exports"
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
    request: Request,
    auth: AuthContext = Depends(require_permission("accounts", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Share or unshare an account (owner-only).

    ``household`` makes the account visible to every household member and
    eligible for the shared Wealthfolio export; ``private`` restricts it
    to the owner (and tenant admins).  Every transition is audit-logged.

    Unsharing (→ ``private``) an account that had already been exported
    does **not** delete anything: the response carries
    ``export_cleanup_required`` plus an ``export_artifacts`` description
    so the UI can ask the owner to explicitly quarantine or delete the
    previously exported data (see ``GET /export-artifacts``,
    ``POST /export-quarantine``, ``POST /export-cleanup``).
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

    response: dict[str, Any] = {
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
        "export_cleanup_required": False,
        "export_artifacts": None,
    }

    # Unsharing an account with prior exports → report what the owner
    # must explicitly quarantine/delete.  Never delete silently here.
    if account.visibility == "private":
        try:
            artifacts = await describe_export_artifacts(
                db,
                tenant_id=auth.tenant_id,
                account_id=account_id,
                actor=auth.user,
                output_dir=_export_output_dir(request),
            )
        except AccountSharingError as exc:
            _raise_sharing_error(exc)
        cleanup_required = bool(
            artifacts["has_mapping"]
            or artifacts["has_delivery_cursor"]
            or artifacts["csv_file_count"] > 0
            or artifacts["quarantined_file_count"] > 0
        )
        response["export_cleanup_required"] = cleanup_required
        if cleanup_required:
            response["export_artifacts"] = artifacts
    return response


@router.get(
    "/{account_id}/export-artifacts",
    response_model=ExportArtifactsResponse,
)
async def get_export_artifacts(
    account_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("accounts", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Describe what was already exported for an account (owner-only).

    Lists the Wealthfolio mapping row, the delivery cursor and the CSV
    files the exporter wrote for this account — read-only, nothing is
    modified.  The UI shows this before asking the owner to confirm
    quarantine or deletion.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        return await describe_export_artifacts(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            actor=auth.user,
            output_dir=_export_output_dir(request),
        )
    except AccountSharingError as exc:
        _raise_sharing_error(exc)


@router.post("/{account_id}/export-quarantine")
async def quarantine_export(
    account_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("accounts", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Quarantine the account's already-exported CSV files (owner-only).

    Non-destructive: files are moved into the exporter's quarantine
    directory and the mapping/delivery rows are kept, so a future
    re-share resumes cleanly.  Audited as ``account_export_quarantine``.
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        result = await quarantine_export_artifacts(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            actor=auth.user,
            output_dir=_export_output_dir(request),
        )
    except AccountSharingError as exc:
        _raise_sharing_error(exc)
    await db.commit()
    return result


@router.post("/{account_id}/export-cleanup")
async def cleanup_export(
    account_id: str,
    body: ExportCleanupRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("accounts", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete the account's already-exported data (owner-only).

    Destructive: removes the CSV files (including quarantined copies)
    and the Wealthfolio mapping/delivery rows.  **Requires explicit
    ``confirm: true``** — a request without confirmation is rejected
    with 422 and touches nothing.  Audited as
    ``account_export_quarantine`` (decision=delete).
    """
    if auth.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot manage account sharing",
        )
    try:
        result = await delete_export_artifacts(
            db,
            tenant_id=auth.tenant_id,
            account_id=account_id,
            actor=auth.user,
            output_dir=_export_output_dir(request),
            confirm=body.confirm,
        )
    except AccountSharingError as exc:
        if exc.code == ERR_CONFIRMATION_REQUIRED:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.message,
            ) from exc
        _raise_sharing_error(exc)
    await db.commit()
    return result


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
