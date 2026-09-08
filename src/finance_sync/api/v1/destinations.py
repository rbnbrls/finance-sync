"""Destination wizard API for optional Wealthfolio, Actual Budget and Jupyter consumers."""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_role
from finance_sync.api.middleware.destination_probe_rate_limit import (
    check_destination_probe_rate_limit,
)
from finance_sync.dependencies import get_container, get_db
from finance_sync.exporter.capabilities import (
    build_destination_preview,
    destination_capabilities,
)
from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.models import WealthfolioAccountMapping
from finance_sync.models import (
    Account,
    ApiKey,
    Holding,
    Security,
    SyncSchedule,
    Transaction,
)
from finance_sync.models.export_target import (
    TARGET_ACTIVE,
    TARGET_ACTUAL_BUDGET,
    TARGET_FIREFLY,
    TARGET_GHOSTFOLIO,
    TARGET_INVESTBRAIN,
    TARGET_JUPYTER,
    TARGET_PAUSED,
    TARGET_SECURO,
    TARGET_TYPES,
    TARGET_YNAB,
    ExportTarget,
)
from finance_sync.models.sync_schedule import SCOPE_EXPORT
from finance_sync.observability.destination_metrics import (
    record_destination_probe,
)
from finance_sync.services.auth import (
    decrypt_credential,
    encrypt_credential,
    generate_api_key,
)
from finance_sync.services.destination_reconciliation import (
    reconcile_destination,
)
from finance_sync.services.sync_schedule import (
    SyncScheduleService,
    compute_next_run,
)
from finance_sync.services.wealthfolio_preflight import (
    missing_wealthfolio_assets,
    probe_wealthfolio_destination,
)
from finance_sync.utils.redaction import sanitize_error

router = APIRouter(prefix="/destinations", tags=["destinations"])
_Admin = Depends(require_role("admin"))
_JUPYTER_DATASETS = [
    "accounts",
    "transactions",
    "holdings",
    "securities",
    "prices",
]
_JUPYTER_PERMISSIONS = (
    "accounts:read transactions:read holdings:read securities:read"
)


class TargetCreate(BaseModel):
    target_type: str = Field(
        description=(
            "wealthfolio, actual-budget, firefly, ghostfolio, "
            "investbrain, securo or jupyter"
        )
    )
    display_name: str = Field(min_length=1, max_length=128)
    configuration: dict[str, object] = Field(default_factory=dict)
    secret: dict[str, str] = Field(default_factory=dict, repr=False)
    selected_account_ids: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=lambda: list(_JUPYTER_DATASETS))

    @field_validator("target_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in TARGET_TYPES:
            msg = f"target_type must be one of {sorted(TARGET_TYPES)}"
            raise ValueError(msg)
        return value


class TargetUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    configuration: dict[str, object] | None = None
    secret: dict[str, str] | None = Field(default=None, repr=False)
    selected_account_ids: list[str] | None = None
    datasets: list[str] | None = None


class TargetResponse(BaseModel):
    id: str
    target_type: str
    display_name: str
    status: str
    version: int
    configuration: dict[str, object]
    selected_account_ids: list[str]
    datasets: list[str]
    has_secret: bool
    jupyter_api_key_id: str | None
    schedule_id: str | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_error: str | None
    last_health_status: str | None
    last_health_error: str | None
    last_parity_summary: dict[str, object] | None = None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DestinationParityCounts(BaseModel):
    """Safe aggregate counts returned by an opt-in destination probe."""

    remote_accounts: int = 0
    unmapped_remote_accounts: int = 0
    remote_assets: int = 0
    remote_activities: int = 0
    canonical_activities: int = 0
    stale_remote_activities: int = 0


class DestinationParityAccount(BaseModel):
    """Safe per-account parity counts; never contains remote payloads."""

    account_id: str
    canonical_activities: int = 0
    remote_activities: int = 0
    missing_activities: int = 0
    stale_remote_activities: int = 0


def _empty_destination_parity_accounts() -> list[DestinationParityAccount]:
    return []


class TestResponse(BaseModel):
    status: str
    message: str
    parity: DestinationParityCounts = Field(
        default_factory=DestinationParityCounts
    )
    parity_accounts: list[DestinationParityAccount] = Field(
        default_factory=_empty_destination_parity_accounts
    )


class ActualBudgetDiscoveryResponse(BaseModel):
    """Metadata-only list used to choose an Actual Budget sync file."""

    budgets: list[dict[str, object]]


class PreviewResponse(BaseModel):
    accounts: list[dict[str, object]]
    account_count: int
    datasets: list[str]
    writes_remote_data: bool = False
    remote_accounts_read: bool = False
    spending: dict[str, object] = Field(default_factory=dict)


class ReconciliationRequest(BaseModel):
    """Native destination records returned by an adapter read operation."""

    records: list[dict[str, Any]] = Field(
        default_factory=lambda: list[dict[str, Any]]()
    )


class JupyterBootstrapResponse(BaseModel):
    api_key: str
    notebook: str


class ActivationResponse(BaseModel):
    target: TargetResponse
    jupyter_bootstrap: JupyterBootstrapResponse | None = None


class DestinationRunResponse(BaseModel):
    status: str
    error: str | None = None


def _wealthfolio_provider_account_id(account: dict[str, Any]) -> str:
    """Read the stable provider account identity from either API spelling."""
    return str(
        account.get("providerAccountId")
        or account.get("provider_account_id")
        or ""
    )


def _missing_wealthfolio_account_mappings(
    mappings: Sequence[Any], remote_provider_ids: set[str]
) -> list[Any]:
    """Return mappings that cannot be proven present in Wealthfolio."""
    return [
        mapping
        for mapping in mappings
        if not mapping.provider_account_id
        or mapping.provider_account_id not in remote_provider_ids
    ]


def _activity_parity_counts(
    canonical_rows: Sequence[tuple[Any, Any]],
    remote_rows: Sequence[dict[str, Any]],
) -> tuple[int, int, int]:
    """Return active canonical, missing canonical and stale remote counts."""
    active_ids: set[str] = set()
    for external_id, tombstoned_at in canonical_rows:
        normalized = str(external_id or "").strip()
        if not normalized:
            continue
        if tombstoned_at is None:
            active_ids.add(normalized)
    remote_ids = {
        value
        for activity in remote_rows
        if (
            value := str(
                activity.get("sourceRecordId")
                or activity.get("externalTransactionId")
                or ""
            ).strip()
        )
    }
    return (
        len(active_ids),
        len(active_ids - remote_ids),
        len(remote_ids - active_ids),
    )


def _parity_summary(status: str, parity: dict[str, int]) -> dict[str, object]:
    """Keep only bounded aggregate parity counts for persistent evidence."""
    allowed = {
        "remote_accounts",
        "unmapped_remote_accounts",
        "remote_assets",
        "remote_activities",
        "canonical_activities",
        "stale_remote_activities",
    }
    counts = {
        key: max(0, int(value))
        for key, value in parity.items()
        if key in allowed
    }
    return {"status": status, "counts": counts}


def _response(
    row: ExportTarget, schedule: SyncSchedule | None = None
) -> TargetResponse:
    return TargetResponse(
        id=str(row.id),
        target_type=row.target_type,
        display_name=row.display_name,
        status=row.status,
        version=int(row.version),
        configuration=dict(row.configuration or {}),
        selected_account_ids=list(row.selected_account_ids or []),
        datasets=list(row.datasets or []),
        has_secret=bool(row.encrypted_secret),
        jupyter_api_key_id=str(row.jupyter_api_key_id)
        if row.jupyter_api_key_id
        else None,
        schedule_id=str(row.schedule_id) if row.schedule_id else None,
        next_run_at=schedule.next_run_at if schedule else None,
        last_run_at=schedule.last_run_at if schedule else None,
        last_run_status=schedule.last_run_status if schedule else None,
        last_run_error=schedule.last_run_error if schedule else None,
        last_health_status=row.last_health_status,
        last_health_error=row.last_health_error,
        last_parity_summary=dict(row.last_parity_summary or {}) or None,
        last_checked_at=row.last_checked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _safe_url(value: object) -> str:
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422, detail="configuration.server_url is required"
        )
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=422,
            detail="Use an absolute HTTPS URL (or private/local HTTP URL)",
        )
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        try:
            private = (
                ipaddress.ip_address(host).is_private
                or ipaddress.ip_address(host).is_loopback
            )
        except ValueError:
            private = host in {
                "localhost",
                "host.docker.internal",
            } or host.endswith(".local")
        if not private:
            raise HTTPException(
                status_code=422,
                detail="HTTP is only allowed for local or private self-hosted servers",
            )
    return value.rstrip("/")


async def _target(
    db: AsyncSession, tenant_id: str, target_id: str
) -> ExportTarget:
    row = await db.scalar(
        select(ExportTarget).where(
            ExportTarget.id == target_id, ExportTarget.tenant_id == tenant_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    return row


async def _validate_accounts(
    db: AsyncSession, tenant_id: str, account_ids: list[str]
) -> None:
    if len(account_ids) != len(set(account_ids)):
        raise HTTPException(
            status_code=422, detail="selected_account_ids must be unique"
        )
    if account_ids:
        count = await db.scalar(
            select(func.count())
            .select_from(Account)
            .where(Account.tenant_id == tenant_id, Account.id.in_(account_ids))
        )
        if count != len(account_ids):
            raise HTTPException(
                status_code=422,
                detail="An account does not belong to this datalake",
            )


async def _jupyter_account_scope(
    db: AsyncSession, tenant_id: str, selected_account_ids: list[str]
) -> list[str]:
    """Materialise the Jupyter key allowlist without exposing account data."""
    if selected_account_ids:
        return list(selected_account_ids)
    account_ids = await db.scalars(
        select(Account.id).where(
            Account.tenant_id == tenant_id,
            Account.is_active.is_(True),  # type: ignore[attr-defined]
        )
    )
    return [str(account_id) for account_id in account_ids]


def _validate_body(body: TargetCreate | TargetUpdate, target_type: str) -> None:
    config = body.configuration
    if target_type != TARGET_JUPYTER and config is not None:
        _safe_url(config.get("server_url"))
    if config is not None:
        secret_keys = {
            key
            for key in config
            if any(
                part in key.lower()
                for part in (
                    "password",
                    "secret",
                    "token",
                    "credential",
                    "api_key",
                    "authorization",
                )
            )
        }
        if secret_keys:
            raise HTTPException(
                status_code=422,
                detail="Put credentials in secret, never configuration",
            )
    if body.datasets is not None and (
        not body.datasets or set(body.datasets) - set(_JUPYTER_DATASETS)
    ):
        raise HTTPException(
            status_code=422,
            detail=f"datasets must be a non-empty subset of {_JUPYTER_DATASETS}",
        )


def _actual_account_mapping_preview(
    accounts: Sequence[tuple[str, str]],
    remote_accounts: list[dict[str, object]],
    *,
    default_off_budget: bool,
) -> list[dict[str, object]]:
    """Describe the non-writing Actual Budget mapping by account name."""
    by_name = {
        str(account["name"]): account
        for account in remote_accounts
        if account.get("name") is not None
    }
    preview: list[dict[str, object]] = []
    for account_id, name in accounts:
        remote = by_name.get(str(name))
        preview.append(
            {
                "id": str(account_id),
                "name": str(name),
                "action": "use_existing" if remote else "create_on_first_sync",
                "actual_budget_account_id": (
                    str(remote["id"]) if remote and remote.get("id") else None
                ),
                "actual_budget_account_name": (
                    str(remote["name"])
                    if remote and remote.get("name") is not None
                    else None
                ),
                "off_budget": (
                    bool(remote.get("offbudget", False))
                    if remote
                    else default_off_budget
                ),
            }
        )
    return preview


@router.get("/types")
async def list_types(_auth: AuthContext = _Admin) -> list[dict[str, object]]:
    """Return metadata used by step one of the wizard."""
    return [
        {
            "key": TARGET_YNAB,
            "name": "YNAB",
            "needs_server": True,
            "secret_label": "Personal access token",
            "datasets": ["accounts", "transactions", "categories"],
        },
        {
            "key": "wealthfolio",
            "name": "Wealthfolio",
            "needs_server": True,
            "datasets": ["accounts", "transactions", "holdings"],
        },
        {
            "key": "actual-budget",
            "name": "Actual Budget",
            "needs_server": True,
            "datasets": ["accounts", "transactions"],
        },
        {
            "key": "jupyter",
            "name": "Jupyter Notebook",
            "needs_server": False,
            "datasets": _JUPYTER_DATASETS,
        },
        {
            "key": TARGET_FIREFLY,
            "name": "Firefly III",
            "needs_server": True,
            "secret_label": "Personal access token",
            "datasets": ["accounts", "transactions"],
        },
        {
            "key": TARGET_GHOSTFOLIO,
            "name": "Ghostfolio",
            "needs_server": True,
            "secret_label": "Access token",
            "datasets": ["transactions", "holdings"],
        },
        {
            "key": TARGET_INVESTBRAIN,
            "name": "InvestBrain",
            "needs_server": True,
            "secret_label": "Access token",
            "datasets": ["transactions", "holdings"],
        },
        {
            "key": TARGET_SECURO,
            "name": "Securo",
            "needs_server": True,
            "secret_label": "Securo-wachtwoord",
            "datasets": ["accounts", "transactions", "holdings"],
        },
    ]


@router.get("/capabilities")
async def list_destination_capabilities(
    _auth: AuthContext = _Admin,
) -> dict[str, dict[str, dict[str, object]]]:
    """Expose destination semantics without leaking destination secrets."""
    return {
        target: {
            name: {
                "mode": capability.mode,
                "bidirectional": capability.bidirectional,
            }
            for name, capability in destination_capabilities(target).items()
        }
        for target in ("wealthfolio", "actual-budget", "ynab", "firefly")
    }


@router.get("", response_model=list[TargetResponse])
async def list_targets(
    auth: AuthContext = _Admin, db: AsyncSession = Depends(get_db)
) -> list[TargetResponse]:
    rows = (
        (
            await db.execute(
                select(ExportTarget)
                .where(ExportTarget.tenant_id == auth.tenant_id)
                .order_by(ExportTarget.display_name)
            )
        )
        .scalars()
        .all()
    )
    schedule_ids = [row.schedule_id for row in rows if row.schedule_id]
    schedules: dict[str, SyncSchedule] = {}
    if schedule_ids:
        schedule_rows = (
            (
                await db.execute(
                    select(SyncSchedule).where(
                        SyncSchedule.tenant_id == auth.tenant_id,
                        SyncSchedule.id.in_(schedule_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        schedules = {str(schedule.id): schedule for schedule in schedule_rows}
    return [_response(row, schedules.get(str(row.schedule_id))) for row in rows]


@router.get("/{target_id}", response_model=TargetResponse)
async def get_target(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> TargetResponse:
    """Return one destination by id (tenant-scoped).

    Read-only counterpart of the wizard's list view; used by the control
    plane's ``configure_destination`` action (GET / destinations:read).
    Credentials are never included.
    """
    row = await _target(db, auth.tenant_id, target_id)
    schedule: SyncSchedule | None = None
    if row.schedule_id:
        schedule = await db.scalar(
            select(SyncSchedule).where(
                SyncSchedule.id == row.schedule_id,
                SyncSchedule.tenant_id == auth.tenant_id,
            )
        )
    return _response(row, schedule)


@router.post(
    "", response_model=TargetResponse, status_code=status.HTTP_201_CREATED
)
async def create_target(
    body: TargetCreate,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> TargetResponse:
    _validate_body(body, body.target_type)
    await _validate_accounts(db, auth.tenant_id, body.selected_account_ids)
    ciphertext = nonce = None
    if body.secret:
        ciphertext, nonce = encrypt_credential(
            json.dumps(body.secret), get_container(request).settings
        )
    row = ExportTarget(
        tenant_id=auth.tenant_id,
        target_type=body.target_type,
        display_name=body.display_name,
        configuration=body.configuration,
        selected_account_ids=body.selected_account_ids,
        datasets=body.datasets,
        encrypted_secret=ciphertext,
        secret_nonce=nonce,
    )
    db.add(row)
    await db.flush()
    return _response(row)


@router.patch("/{target_id}", response_model=TargetResponse)
async def update_target(
    target_id: str,
    body: TargetUpdate,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> TargetResponse:
    row = await _target(db, auth.tenant_id, target_id)
    _validate_body(body, row.target_type)
    if body.selected_account_ids is not None:
        await _validate_accounts(db, auth.tenant_id, body.selected_account_ids)
        row.selected_account_ids = body.selected_account_ids
        if row.target_type == TARGET_JUPYTER and row.jupyter_api_key_id:
            key = await db.scalar(
                select(ApiKey).where(
                    ApiKey.id == row.jupyter_api_key_id,
                    ApiKey.tenant_id == auth.tenant_id,
                )
            )
            if key is not None:
                key.account_scope = await _jupyter_account_scope(
                    db, auth.tenant_id, body.selected_account_ids
                )
    for field in ("display_name", "configuration", "datasets"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    if body.secret is not None:
        row.encrypted_secret, row.secret_nonce = encrypt_credential(
            json.dumps(body.secret), get_container(request).settings
        )
    row.version += 1
    await db.flush()
    return _response(row)


@router.post("/{target_id}/preview", response_model=PreviewResponse)
async def preview_target(
    target_id: str,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> PreviewResponse:
    row = await _target(db, auth.tenant_id, target_id)
    stmt = select(Account.id, Account.name).where(
        Account.tenant_id == auth.tenant_id
    )
    if row.selected_account_ids:
        stmt = stmt.where(Account.id.in_(row.selected_account_ids))
    accounts = (await db.execute(stmt.order_by(Account.name))).all()
    account_rows = [
        (str(account_id), str(name)) for account_id, name in accounts
    ]
    preview_accounts: list[dict[str, object]] = [
        {"id": account_id, "name": name} for account_id, name in account_rows
    ]
    transaction_stmt = select(Transaction).where(
        Transaction.tenant_id == auth.tenant_id,
        Transaction.account_id.in_(
            [account_id for account_id, _ in account_rows]
        ),
    )
    transactions = list((await db.execute(transaction_stmt)).scalars().all())
    remote_accounts_read = False
    if (
        row.target_type == TARGET_ACTUAL_BUDGET
        and row.encrypted_secret
        and row.secret_nonce
    ):
        try:
            from finance_sync.exporter.actual_budget.client import (
                ActualBudgetClient,
            )
            from finance_sync.exporter.actual_budget.config import (
                ActualBudgetConfig,
            )

            plaintext = decrypt_credential(
                row.encrypted_secret,
                row.secret_nonce,
                get_container(request).settings,
            )
            secret = json.loads(plaintext)
            config = ActualBudgetConfig(
                server_url=_safe_url(
                    (row.configuration or {}).get("server_url")
                ),
                password=str(secret.get("password") or ""),
                budget_name=(row.configuration or {}).get("budget_name")
                or None,
                sync_id=(row.configuration or {}).get("sync_id") or None,
                encryption_password=secret.get("encryption_password") or None,
                default_off_budget=bool(
                    (row.configuration or {}).get("default_off_budget", False)
                ),
            )
            async with ActualBudgetClient(config) as client:
                remote_accounts = await client.get_accounts()
            preview_accounts = _actual_account_mapping_preview(
                account_rows,
                remote_accounts,
                default_off_budget=config.default_off_budget,
            )
            remote_accounts_read = True
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Actual Budget mapping preview failed: "
                    f"{sanitize_error(str(exc))}"
                ),
            ) from exc
    return PreviewResponse(
        accounts=preview_accounts,
        account_count=len(accounts),
        datasets=list(row.datasets or []),
        remote_accounts_read=remote_accounts_read,
        spending=build_destination_preview(
            row.target_type, transactions, preview_accounts
        ),
    )


@router.post("/{target_id}/reconciliation")
async def reconcile_target(
    target_id: str,
    body: ReconciliationRequest,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Compare adapter-provided native records with tenant canonical data.

    Reading a destination is deliberately adapter-owned.  The endpoint
    accepts that read result and performs only a tenant-scoped, side-effect
    free comparison; it never treats destination IDs as source IDs.
    """
    row = await _target(db, auth.tenant_id, target_id)
    account_stmt = select(Account.id).where(Account.tenant_id == auth.tenant_id)
    if row.selected_account_ids:
        account_stmt = account_stmt.where(
            Account.id.in_(row.selected_account_ids)
        )
    account_ids = [
        account_id for (account_id,) in (await db.execute(account_stmt)).all()
    ]
    transaction_stmt = select(Transaction).where(
        Transaction.tenant_id == auth.tenant_id,
        Transaction.account_id.in_(account_ids),
    )
    canonical = list((await db.execute(transaction_stmt)).scalars().all())
    findings = reconcile_destination(
        canonical,
        body.records,
        destination_type=row.target_type,
    )
    return {
        "target_id": str(row.id),
        "destination": row.target_type,
        "canonical_count": len(canonical),
        "destination_count": len(body.records),
        "finding_count": len(findings),
        "findings": findings,
    }


@router.post(
    "/{target_id}/actual-budgets",
    response_model=ActualBudgetDiscoveryResponse,
)
async def discover_actual_budgets(
    target_id: str,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> ActualBudgetDiscoveryResponse:
    """Discover selectable Actual budgets without downloading or writing one."""
    row = await _target(db, auth.tenant_id, target_id)
    if row.target_type != TARGET_ACTUAL_BUDGET:
        raise HTTPException(
            status_code=409,
            detail="Budget discovery is only available for Actual Budget",
        )
    if not row.encrypted_secret or not row.secret_nonce:
        raise HTTPException(
            status_code=422,
            detail="Enter the Actual Budget server password before discovery",
        )
    try:
        from finance_sync.exporter.actual_budget.client import (
            ActualBudgetClient,
        )
        from finance_sync.exporter.actual_budget.config import (
            ActualBudgetConfig,
        )

        secret = json.loads(
            decrypt_credential(
                row.encrypted_secret,
                row.secret_nonce,
                get_container(request).settings,
            )
        )
        budgets = await ActualBudgetClient.discover_budgets(
            ActualBudgetConfig(
                server_url=_safe_url(
                    (row.configuration or {}).get("server_url")
                ),
                password=str(secret.get("password") or ""),
            )
        )
        row.last_health_status, row.last_health_error = "ready", None
        row.last_checked_at = datetime.now(UTC)
        await db.flush()
        return ActualBudgetDiscoveryResponse(budgets=budgets)
    except HTTPException:
        raise
    except Exception as exc:
        row.last_health_status, row.last_health_error = (
            "failed",
            sanitize_error(str(exc)),
        )
        row.last_checked_at = datetime.now(UTC)
        await db.flush()
        raise HTTPException(
            status_code=422,
            detail="Actual Budget discovery failed; verify the server and password",
        ) from exc


@router.post("/{target_id}/test", response_model=TestResponse)
async def test_target(
    target_id: str,
    request: Request,
    auth: AuthContext = _Admin,
    _rate_limit: None = Depends(check_destination_probe_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> TestResponse:
    """Validate configuration without creating remote accounts or transactions.

    The probe authenticates and only reads remote metadata; it never creates
    remote accounts, transactions, activities or holdings.
    """
    row = await _target(db, auth.tenant_id, target_id)
    parity: dict[str, int] = {}
    parity_accounts: list[DestinationParityAccount] = []

    def parity_response() -> DestinationParityCounts:
        return DestinationParityCounts(
            remote_accounts=parity.get("remote_accounts", 0),
            unmapped_remote_accounts=parity.get(
                "unmapped_remote_accounts", 0
            ),
            remote_assets=parity.get("remote_assets", 0),
            remote_activities=parity.get("remote_activities", 0),
            canonical_activities=parity.get("canonical_activities", 0),
            stale_remote_activities=parity.get(
                "stale_remote_activities", 0
            ),
        )

    probe_started = time.perf_counter()

    def record_health(status: str, error: str | None = None) -> None:
        """Persist status plus aggregate-only parity evidence."""
        row.last_health_status = status
        row.last_health_error = error
        row.last_parity_summary = _parity_summary(status, parity)
        row.last_checked_at = datetime.now(UTC)
        record_destination_probe(
            destination=row.target_type,
            status=status,
            duration_seconds=time.perf_counter() - probe_started,
            parity=parity,
        )

    if not get_container(request).settings.destination_remote_probe_enabled:
        record_health("disabled", "remote_probe_disabled")
        await db.flush()
        return TestResponse(
            status="disabled",
            message="Remote destination probe is disabled by configuration.",
        )

    try:
        if row.target_type != TARGET_JUPYTER:
            server_url = _safe_url((row.configuration or {}).get("server_url"))
            if not row.encrypted_secret or not row.secret_nonce:
                msg = (
                    "Enter the server credential before testing the connection"
                )
                raise ValueError(msg)
            plaintext = decrypt_credential(
                row.encrypted_secret,
                row.secret_nonce,
                get_container(request).settings,
            )
            password = json.loads(plaintext).get("password", "")
            if row.target_type == "wealthfolio":
                from finance_sync.exporter.wealthfolio.client import (
                    WealthfolioClient,
                    WealthfolioClientConfig,
                )

                wf_config = WealthfolioClientConfig(
                    base_url=server_url, password=password
                )
                async with WealthfolioClient(wf_config) as client:
                    probe = await probe_wealthfolio_destination(
                        client,
                        timeout_seconds=5.0,
                        include_activities=True,
                    )
                    if probe.status != "ready":
                        record_health(probe.status, probe.reason)
                        await db.flush()
                        return TestResponse(
                            status=probe.status,
                            message=probe.reason
                            or "Wealthfolio destination unavailable",
                        )
                    parity = {
                        "remote_accounts": len(probe.accounts),
                        "remote_assets": len(probe.assets),
                        "remote_activities": sum(
                            len(rows) for rows in probe.activities.values()
                        ),
                    }
                    mappings = list(
                        (
                            await db.execute(
                                select(WealthfolioAccountMapping).where(
                                    WealthfolioAccountMapping.tenant_id
                                    == auth.tenant_id,
                                    WealthfolioAccountMapping.target_id
                                    == str(row.id),
                                )
                            )
                        ).scalars()
                    )
                    remote_provider_ids = {
                        provider_id
                        for account in probe.accounts
                        if (
                            provider_id := _wealthfolio_provider_account_id(
                                account
                            )
                        )
                    }
                    mapped_provider_ids = {
                        str(mapping.provider_account_id or "").strip()
                        for mapping in mappings
                        if str(mapping.provider_account_id or "").strip()
                    }
                    unmapped_remote_accounts = (
                        remote_provider_ids - mapped_provider_ids
                    )
                    parity["unmapped_remote_accounts"] = len(
                        unmapped_remote_accounts
                    )
                    missing_mappings = _missing_wealthfolio_account_mappings(
                        mappings, remote_provider_ids
                    )
                    if missing_mappings:
                        error = (
                            f"{len(missing_mappings)} mapped account(s) "
                            "are incomplete or were not found remotely"
                        )
                        record_health("degraded", error)
                        await db.flush()
                        return TestResponse(
                            status="degraded",
                            message=error,
                            parity=parity_response(),
                        )
                    if unmapped_remote_accounts:
                        error = (
                            f"{len(unmapped_remote_accounts)} remote account(s) "
                            "are not mapped to a canonical account"
                        )
                        record_health("degraded", error)
                        await db.flush()
                        return TestResponse(
                            status="degraded",
                            message=error,
                            parity=parity_response(),
                        )
                    canonical_security_rows = list(
                        (
                            await db.execute(
                                select(Security).where(
                                    or_(
                                        Security.id.in_(
                                            select(Holding.security_id).where(
                                                Holding.tenant_id
                                                == auth.tenant_id
                                            )
                                        ),
                                        Security.id.in_(
                                            select(
                                                Transaction.security_id
                                            ).where(
                                                Transaction.tenant_id
                                                == auth.tenant_id,
                                                Transaction.security_id.is_not(
                                                    None
                                                ),
                                            )
                                        ),
                                    )
                                )
                            )
                        ).scalars()
                    )
                    missing_assets = missing_wealthfolio_assets(
                        canonical_security_rows,
                        probe.assets,
                    )
                    if missing_assets:
                        error = (
                            f"{len(missing_assets)} canonical asset(s) "
                            "were not found remotely"
                        )
                        record_health("degraded", error)
                        await db.flush()
                        return TestResponse(
                            status="degraded",
                            message=error,
                            parity=parity_response(),
                        )
                    account_ids = [
                        str(mapping.account_id) for mapping in mappings
                    ]
                    canonical_rows = list(
                        (
                            await db.execute(
                                select(
                                    Transaction.account_id,
                                    Transaction.external_transaction_id,
                                    Transaction.tombstoned_at,
                                ).where(
                                    Transaction.tenant_id == auth.tenant_id,
                                    Transaction.account_id.in_(account_ids),
                                )
                            )
                        ).all()
                    )
                    remote_ids_by_account = {
                        _wealthfolio_provider_account_id(account): str(
                            account.get("id") or ""
                        )
                        for account in probe.accounts
                    }
                    missing_activity_count = 0
                    stale_activity_count = 0
                    canonical_by_account: dict[str, list[tuple[Any, Any]]] = {}
                    for (
                        account_id,
                        external_id,
                        tombstoned_at,
                    ) in canonical_rows:
                        canonical_by_account.setdefault(
                            str(account_id), []
                        ).append((external_id, tombstoned_at))
                    for mapping in mappings:
                        account_id = str(mapping.account_id)
                        provider_id = next(
                            (
                                mapping.provider_account_id
                                for mapping in mappings
                                if str(mapping.account_id) == account_id
                            ),
                            None,
                        )
                        remote_account_id = remote_ids_by_account.get(
                            str(provider_id or "")
                        )
                        remote_rows = probe.activities.get(
                            remote_account_id or "", ()
                        )
                        (
                            canonical_count,
                            missing_count,
                            stale_count,
                        ) = _activity_parity_counts(
                            canonical_by_account.get(account_id, []),
                            list(remote_rows),
                        )
                        missing_activity_count += missing_count
                        stale_activity_count += stale_count
                        parity_accounts.append(
                            DestinationParityAccount(
                                account_id=account_id,
                                canonical_activities=canonical_count,
                                remote_activities=len(remote_rows),
                                missing_activities=missing_count,
                                stale_remote_activities=stale_count,
                            )
                        )
                    parity["canonical_activities"] = sum(
                        item.canonical_activities for item in parity_accounts
                    )
                    parity["stale_remote_activities"] = stale_activity_count
                    if missing_activity_count:
                        error = (
                            f"{missing_activity_count} canonical activity(s) "
                            "were not found remotely"
                        )
                        record_health("degraded", error)
                        await db.flush()
                        return TestResponse(
                            status="degraded",
                            message=error,
                            parity=parity_response(),
                            parity_accounts=parity_accounts,
                        )
                    if stale_activity_count:
                        error = (
                            f"{stale_activity_count} remote activity(s) "
                            "are not present in the canonical dataset"
                        )
                        record_health("degraded", error)
                        await db.flush()
                        return TestResponse(
                            status="degraded",
                            message=error,
                            parity=parity_response(),
                            parity_accounts=parity_accounts,
                        )
            elif row.target_type == TARGET_ACTUAL_BUDGET:
                from finance_sync.exporter.actual_budget.client import (
                    ActualBudgetClient,
                )
                from finance_sync.exporter.actual_budget.config import (
                    ActualBudgetConfig,
                )

                ab_config = ActualBudgetConfig(
                    server_url=server_url,
                    password=password,
                    budget_name=(row.configuration or {}).get("budget_name")
                    or None,
                    sync_id=(row.configuration or {}).get("sync_id") or None,
                    encryption_password=json.loads(plaintext).get(
                        "encryption_password"
                    )
                    or None,
                    default_off_budget=bool(
                        (row.configuration or {}).get(
                            "default_off_budget", False
                        )
                    ),
                )
                async with ActualBudgetClient(ab_config) as client:
                    await client.get_accounts()
            elif row.target_type == TARGET_FIREFLY:
                from finance_sync.exporter.firefly.client import (
                    FireflyClient,
                    FireflyClientConfig,
                )

                async with FireflyClient(
                    FireflyClientConfig(
                        base_url=server_url,
                        access_token=str(
                            json.loads(plaintext).get("access_token") or ""
                        ),
                    )
                ) as client:
                    await client.about()
            elif row.target_type == TARGET_GHOSTFOLIO:
                from finance_sync.exporter.ghostfolio.client import (
                    GhostfolioClient,
                )
                from finance_sync.exporter.ghostfolio.config import (
                    GhostfolioConfig,
                )

                async with GhostfolioClient(
                    GhostfolioConfig(
                        server_url=server_url,
                        access_token=str(
                            json.loads(plaintext).get("access_token") or ""
                        ),
                    )
                ) as client:
                    await client.health()
            elif row.target_type == TARGET_INVESTBRAIN:
                from finance_sync.exporter.investbrain.client import (
                    InvestBrainClient,
                )
                from finance_sync.exporter.investbrain.config import (
                    InvestBrainConfig,
                )

                async with InvestBrainClient(
                    InvestBrainConfig(
                        server_url=server_url,
                        access_token=str(
                            json.loads(plaintext).get("access_token") or ""
                        ),
                    )
                ) as client:
                    await client.health()
            elif row.target_type == TARGET_SECURO:
                from finance_sync.exporter.securo.client import SecuroClient
                from finance_sync.exporter.securo.config import SecuroConfig

                secret = json.loads(plaintext)
                config = SecuroConfig(
                    server_url=server_url,
                    email=str((row.configuration or {}).get("email") or ""),
                    password=str(secret.get("password") or ""),
                )
                async with SecuroClient(config) as client:
                    await client.login()
        record_health("ready")
        await db.flush()
        return TestResponse(
            status="ready",
            message="Verbinding en authenticatie werken; er is geen externe data geschreven.",
            parity=parity_response(),
            parity_accounts=parity_accounts,
        )
    except Exception as exc:
        error = sanitize_error(str(exc))
        record_health("failed", error)
        await db.flush()
        if isinstance(exc, HTTPException):
            raise
        return TestResponse(status="failed", message=error or "Test failed")


@router.post("/{target_id}/activate", response_model=ActivationResponse)
async def activate_target(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> ActivationResponse:
    row = await _target(db, auth.tenant_id, target_id)
    if row.target_type != TARGET_JUPYTER and not row.encrypted_secret:
        raise HTTPException(
            status_code=422,
            detail="Test and save a credential before activation",
        )
    bootstrap = None
    schedule: SyncSchedule | None = None
    if row.target_type == TARGET_JUPYTER and not row.jupyter_api_key_id:
        raw, key_hash, prefix = generate_api_key()
        key = ApiKey(
            tenant_id=auth.tenant_id,
            user_id=auth.principal_id,
            name=f"Jupyter: {row.display_name}",
            key_prefix=prefix,
            key_hash=key_hash,
            permissions=_JUPYTER_PERMISSIONS,
            account_scope=await _jupyter_account_scope(
                db, auth.tenant_id, list(row.selected_account_ids or [])
            ),
        )
        db.add(key)
        await db.flush()
        row.jupyter_api_key_id = key.id
        bootstrap = JupyterBootstrapResponse(
            api_key=raw, notebook=_jupyter_notebook()
        )
    row.status = TARGET_ACTIVE
    row.version += 1
    if row.target_type != TARGET_JUPYTER:
        schedule = await SyncScheduleService(db).ensure_for_scope(
            auth.tenant_id,
            scope=SCOPE_EXPORT,
            target_id=f"{row.target_type}:{row.id}",
            actor_user_id=auth.principal_id,
        )
        schedule.enabled = True
        schedule.next_run_at = (compute_next_run(schedule, count=1) or [None])[
            0
        ]
        row.schedule_id = schedule.id
    await db.flush()
    return ActivationResponse(
        target=_response(row, schedule),
        jupyter_bootstrap=bootstrap,
    )


@router.post("/{target_id}/pause", response_model=TargetResponse)
async def pause_target(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> TargetResponse:
    row = await _target(db, auth.tenant_id, target_id)
    row.status = TARGET_PAUSED
    row.version += 1
    schedule: SyncSchedule | None = None
    if row.schedule_id:
        schedule = await db.scalar(
            select(SyncSchedule).where(
                SyncSchedule.id == row.schedule_id,
                SyncSchedule.tenant_id == auth.tenant_id,
            )
        )
        if schedule is not None:
            schedule.enabled = False
    await db.flush()
    return _response(row, schedule)


@router.post(
    "/{target_id}/jupyter-key/rotate",
    response_model=JupyterBootstrapResponse,
)
async def rotate_jupyter_key(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> JupyterBootstrapResponse:
    """Rotate a Jupyter consumer credential and return its plaintext once."""
    row = await _target(db, auth.tenant_id, target_id)
    if row.target_type != TARGET_JUPYTER or row.status != TARGET_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="Activate a Jupyter destination before rotating its key",
        )
    if row.jupyter_api_key_id:
        previous = await db.scalar(
            select(ApiKey).where(
                ApiKey.id == row.jupyter_api_key_id,
                ApiKey.tenant_id == auth.tenant_id,
            )
        )
        if previous is not None:
            previous.is_active = False
    raw, key_hash, prefix = generate_api_key()
    key = ApiKey(
        tenant_id=auth.tenant_id,
        user_id=auth.principal_id,
        name=f"Jupyter: {row.display_name}",
        key_prefix=prefix,
        key_hash=key_hash,
        permissions=_JUPYTER_PERMISSIONS,
        account_scope=await _jupyter_account_scope(
            db, auth.tenant_id, list(row.selected_account_ids or [])
        ),
    )
    db.add(key)
    await db.flush()
    row.jupyter_api_key_id = key.id
    row.version += 1
    await db.flush()
    return JupyterBootstrapResponse(api_key=raw, notebook=_jupyter_notebook())


@router.get("/{target_id}/jupyter-notebook", response_class=PlainTextResponse)
async def download_jupyter_notebook(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Download the versioned, credential-free Jupyter starter notebook."""
    row = await _target(db, auth.tenant_id, target_id)
    if row.target_type != TARGET_JUPYTER:
        raise HTTPException(
            status_code=404, detail="Only Jupyter destinations have a notebook"
        )
    return PlainTextResponse(
        _jupyter_notebook(),
        headers={
            "Content-Disposition": (
                'attachment; filename="finance-sync-datalake-starter.py"'
            ),
            "X-Finance-Sync-Consumer-Contract": "v1",
        },
    )


@router.post("/{target_id}/run", response_model=DestinationRunResponse)
async def run_target(
    target_id: str,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> DestinationRunResponse:
    """Manually execute one active app destination through its saved target."""
    row = await _target(db, auth.tenant_id, target_id)
    if row.target_type == TARGET_JUPYTER:
        raise HTTPException(
            status_code=409,
            detail="Jupyter reads the datalake directly and has no export run",
        )
    if row.status != TARGET_ACTIVE or not row.schedule_id:
        raise HTTPException(
            status_code=409, detail="Activate this destination first"
        )
    schedule = await db.scalar(
        select(SyncSchedule).where(
            SyncSchedule.id == row.schedule_id,
            SyncSchedule.tenant_id == auth.tenant_id,
        )
    )
    if schedule is None:
        raise HTTPException(
            status_code=409, detail="Destination schedule is missing"
        )
    from finance_sync.worker.schedule_runner import run_export

    result = await run_export(get_container(request), schedule=schedule)
    schedule.last_run_at = datetime.now(UTC)
    schedule.last_run_status = str(result.get("status", "failed"))[:16]
    schedule.last_run_error = (
        sanitize_error(str(result.get("error") or ""))[:500] or None
    )
    await db.flush()
    return DestinationRunResponse(
        status=str(result.get("status", "failed")),
        error=result.get("error"),
    )


@router.post("/{target_id}/retry", response_model=DestinationRunResponse)
async def retry_target(
    target_id: str,
    request: Request,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> DestinationRunResponse:
    """Retry a failed destination through its persisted target contract."""
    latest_run = await db.scalar(
        select(ExportRun)
        .where(
            ExportRun.tenant_id == auth.tenant_id,
            ExportRun.target_id == target_id,
        )
        .order_by(ExportRun.started_at.desc())
        .limit(1)
    )
    if latest_run is None or latest_run.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only destinations with a failed export can be retried",
        )
    return await run_target(target_id, request, auth, db)


def _jupyter_notebook() -> str:
    """Return a starter notebook body; the caller supplies the one-time key."""
    return """# finance-sync personal datalake starter — consumer contract v1
import os
import requests

BASE_URL = os.environ['FINANCE_SYNC_URL'].rstrip('/')
TOKEN = os.environ['FINANCE_SYNC_JUPYTER_TOKEN']
HEADERS = {'X-API-Key': TOKEN}

def read_dataset(path, **params):
    response = requests.get(
        f'{BASE_URL}/api/v1/{path}', headers=HEADERS, params=params, timeout=30
    )
    response.raise_for_status()
    return response.json()

# All calls are read-only. Responses retain finance-sync timestamps and
# provenance fields supplied by the stable data API.
accounts = read_dataset('accounts')
transactions = read_dataset('transactions')
holdings = read_dataset('holdings')
securities = read_dataset('securities')
prices = read_dataset('prices')
datasets = {
    'accounts': accounts,
    'transactions': transactions,
    'holdings': holdings,
    'securities': securities,
    'prices': prices,
}
datasets
"""


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: str,
    auth: AuthContext = _Admin,
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await _target(db, auth.tenant_id, target_id)
    if row.jupyter_api_key_id:
        key = await db.scalar(
            select(ApiKey).where(
                ApiKey.id == row.jupyter_api_key_id,
                ApiKey.tenant_id == auth.tenant_id,
            )
        )
        if key is not None:
            key.is_active = False
    if row.schedule_id:
        schedule = await db.scalar(
            select(SyncSchedule).where(
                SyncSchedule.id == row.schedule_id,
                SyncSchedule.tenant_id == auth.tenant_id,
            )
        )
        if schedule is not None:
            await db.delete(schedule)
    await db.delete(row)
    await db.flush()
