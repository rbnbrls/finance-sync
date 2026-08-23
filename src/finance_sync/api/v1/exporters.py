"""REST API for managing exporters and triggering export runs.

Allows authenticated users to view exporter configuration (read from
environment variables / Settings) and trigger export runs to Wealthfolio.
"""

from __future__ import annotations

from datetime import (
    datetime,
)
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from finance_sync.api.deps.auth import AuthContext, get_auth_context
from finance_sync.dependencies import get_container, get_db
from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
from finance_sync.exporter.actual_budget.exporter import ActualBudgetExporter
from finance_sync.exporter.firefly.config import FireflyConfig
from finance_sync.exporter.firefly.exporter import (
    FireflyExporter,
    FireflyExportResult,
)
from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
from finance_sync.exporter.wealthfolio.exporter import (
    WealthfolioExporter,
    WealthfolioExportResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
    )

router = APIRouter(prefix="/exporters", tags=["exporters"])

# ── Pydantic schemas ─────────────────────────────────────────────────────


class ExporterTypeInfo(BaseModel):
    """Public info about an available exporter type."""

    name: str = Field(description="Exporter key, e.g. 'wealthfolio'")
    display_name: str = Field(description="Human-readable name")
    description: str = Field(description="Brief description of the exporter")
    config_fields: list[dict[str, object]] = Field(
        default_factory=list[dict[str, object]],
        description="Configuration fields exposed in Settings",
    )


class ExporterConfigResponse(BaseModel):
    """Current exporter configuration (read from environment)."""

    exporter_type: str = Field(description="Exporter key")
    output_dir: str = Field(description="Directory for CSV export files")
    default_currency: str = Field(description="Default currency code")
    export_holdings: bool = Field(
        description="Generate holdings-mode CSV snapshot"
    )
    include_pending: bool = Field(
        description="Include pending (unsettled) transactions"
    )
    account_name_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Account name overrides",
    )
    instrument_type_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Instrument type mapping overrides",
    )


class ExportRunResponse(BaseModel):
    """Summary of an export run."""

    id: str
    exporter_type: str | None
    status: str
    started_at: datetime
    completed_at: datetime | None
    transactions_attempted: int | None
    transactions_exported: int | None
    transactions_failed: int | None
    error_message: str | None


class RetryExportResponse(BaseModel):
    """Result of retrying a failed export run.

    ``run_id`` refers to the **new** run created by the retry; the
    original failed run is left untouched for audit.
    """

    run_id: str
    status: str
    transactions_attempted: int | None
    transactions_exported: int | None
    transactions_failed: int | None
    error_message: str | None
    duration_s: float


class TriggerExportResponse(BaseModel):
    """Result after triggering an export run."""

    status: str
    accounts_mapped: int
    transactions_attempted: int
    transactions_exported: int
    transactions_failed: int
    transactions_skipped: int
    holdings_exported: int
    csv_files: list[str]
    duration_s: float
    error_message: str | None


class ExportRunsListResponse(BaseModel):
    """Paginated list of export runs."""

    runs: list[ExportRunResponse]
    total: int


ExportRunsListResponse.model_rebuild()


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_run_id(run_id: str) -> UUID | None:
    """Parse a run id path param into a UUID for ORM comparison.

    Returns ``None`` when the value is not a valid UUID, so callers can
    treat it as a not-found run.
    """
    try:
        return UUID(str(run_id))
    except ValueError:
        return None


def _build_wealthfolio_config(container: Any) -> WealthfolioConfig:
    """Build a WealthfolioConfig from the application container settings."""
    settings = container.settings
    return WealthfolioConfig(
        output_dir=Path(
            getattr(settings, "wealthfolio_output_dir", "")
            or "/tmp/finance_sync_wealthfolio_exports"
        ),
        default_currency=getattr(
            settings, "wealthfolio_default_currency", "EUR"
        ),
        export_holdings=getattr(settings, "wealthfolio_export_holdings", True),
        max_transactions_per_file=getattr(
            settings, "wealthfolio_max_transactions_per_file", 10_000
        ),
        include_pending=getattr(settings, "wealthfolio_include_pending", False),
        account_name_overrides=getattr(
            settings, "wealthfolio_account_name_overrides", {}
        ),
        instrument_type_overrides=getattr(
            settings, "wealthfolio_instrument_type_overrides", {}
        ),
        holdings_strategy=getattr(
            settings, "wealthfolio_holdings_strategy", "reconcile"
        ),
        reconciliation_absolute_tolerance=getattr(
            settings,
            "wealthfolio_reconciliation_absolute_tolerance",
            Decimal("1.00"),
        ),
        reconciliation_percentage_tolerance=getattr(
            settings,
            "wealthfolio_reconciliation_percentage_tolerance",
            Decimal("0.005"),
        ),
    )


def _require_wealthfolio_enabled(request: Request) -> None:
    """FastAPI dependency: ensure the Wealthfolio exporter is enabled.

    Place before the auth dependency so it runs first.
    """
    settings = get_container(request).settings
    if not settings.exporter_wealthfolio_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Wealthfolio exporter is disabled."
                " Set EXPORTER_WEALTHFOLIO_ENABLED=true to enable."
            ),
        )


# ── Endpoints ───────────────────────────────────────────────────────────


@router.get("/types", response_model=list[ExporterTypeInfo])
async def list_exporter_types(request: Request) -> list[ExporterTypeInfo]:
    """List the available exporter types with their metadata.

    Only exporters whose feature flag is enabled are listed.
    """
    settings = get_container(request).settings
    types: list[ExporterTypeInfo] = []

    if settings.exporter_wealthfolio_enabled:
        types.append(
            ExporterTypeInfo(
                name="wealthfolio",
                display_name="Wealthfolio",
                description=(
                    "Export holdings, trades, and investment transactions "
                    "to Wealthfolio CSV format for portfolio tracking."
                ),
                config_fields=[
                    {
                        "key": "output_dir",
                        "label": "Output Directory",
                        "type": "text",
                        "default": ("/tmp/finance_sync_wealthfolio_exports"),
                        "description": (
                            "Directory for generated CSV export files"
                        ),
                    },
                    {
                        "key": "default_currency",
                        "label": "Default Currency",
                        "type": "text",
                        "default": "EUR",
                        "description": (
                            "Default currency for accounts without "
                            "explicit currency"
                        ),
                    },
                    {
                        "key": "export_holdings",
                        "label": "Export Holdings",
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Generate holdings-mode CSV snapshot "
                            "of current positions"
                        ),
                    },
                    {
                        "key": "include_pending",
                        "label": "Include Pending",
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include pending (unsettled) transactions"
                        ),
                    },
                ],
            )
        )

    if settings.exporter_actual_budget_enabled:
        types.append(
            ExporterTypeInfo(
                name="actual-budget",
                display_name="Actual Budget",
                description=(
                    "Export finance-sync accounts and transactions to "
                    "an Actual Budget server (or CSV summary for manual "
                    "import)."
                ),
                config_fields=[],
            )
        )

    if settings.exporter_firefly_enabled:
        types.append(
            ExporterTypeInfo(
                name="firefly",
                display_name="Firefly III",
                description=(
                    "Push accounts and transactions to a local or remote "
                    "Firefly III instance through its v1 API."
                ),
                config_fields=[
                    {
                        "key": "server_url",
                        "label": "Server URL",
                        "type": "url",
                        "default": "http://localhost:8082",
                    },
                    {
                        "key": "access_token",
                        "label": "Personal access token",
                        "type": "secret",
                    },
                    {
                        "key": "default_currency",
                        "label": "Default currency",
                        "type": "text",
                        "default": "EUR",
                    },
                ],
            )
        )

    return types


def _require_firefly_enabled(request: Request) -> None:
    if not get_container(request).settings.exporter_firefly_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Firefly exporter is disabled. Set EXPORTER_FIREFLY_ENABLED=true.",
        )


@router.get("/firefly/config")
async def get_firefly_config(
    request: Request,
    _flag: None = Depends(_require_firefly_enabled),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Return non-secret Firefly exporter configuration."""
    config = FireflyConfig.from_settings(get_container(request).settings)
    return {
        "exporter_type": "firefly",
        "server_url": config.server_url,
        "default_currency": config.default_currency,
        "import_tag": config.import_tag,
        "configured": bool(config.access_token),
    }


@router.post(
    "/firefly/export",
    response_model=TriggerExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_firefly_export(
    request: Request,
    _flag: None = Depends(_require_firefly_enabled),
    auth: AuthContext = Depends(get_auth_context),
) -> TriggerExportResponse:
    """Run an idempotent finance-sync → Firefly III export."""
    container = get_container(request)
    outcome: FireflyExportResult = await FireflyExporter(
        session_factory=container.session_factory,
        firefly_config=FireflyConfig.from_settings(container.settings),
        tenant_id=auth.tenant_id,
    ).run_export()
    return TriggerExportResponse(
        status=outcome.status,
        accounts_mapped=outcome.accounts_mapped,
        transactions_attempted=outcome.transactions_attempted,
        transactions_exported=outcome.transactions_exported,
        transactions_failed=outcome.transactions_failed,
        transactions_skipped=0,
        holdings_exported=0,
        csv_files=[],
        duration_s=outcome.duration_s,
        error_message=outcome.error_message,
    )


@router.get("/config", response_model=ExporterConfigResponse)
async def get_exporter_config(
    request: Request,
    _flag: None = Depends(_require_wealthfolio_enabled),
    _auth: AuthContext = Depends(get_auth_context),
) -> ExporterConfigResponse:
    """Get the current exporter configuration."""
    container = get_container(request)
    wf_config = _build_wealthfolio_config(container)

    return ExporterConfigResponse(
        exporter_type="wealthfolio",
        output_dir=str(wf_config.output_dir),
        default_currency=wf_config.default_currency,
        export_holdings=wf_config.export_holdings,
        include_pending=wf_config.include_pending,
        account_name_overrides=wf_config.account_name_overrides,
        instrument_type_overrides=wf_config.instrument_type_overrides,
    )


@router.post(
    "/export",
    response_model=TriggerExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_export(
    request: Request,
    _flag: None = Depends(_require_wealthfolio_enabled),
    auth: AuthContext = Depends(get_auth_context),
) -> TriggerExportResponse:
    """Trigger a Wealthfolio export run.

    Exports transactions and holdings from finance-sync to Wealthfolio
    CSV files in the configured output directory.
    """
    container = get_container(request)
    wf_config = _build_wealthfolio_config(container)

    exporter = WealthfolioExporter(
        session_factory=container.session_factory,
        wf_config=wf_config,
        tenant_id=auth.tenant_id,
    )

    result: WealthfolioExportResult = await exporter.run_export()

    return TriggerExportResponse(
        status=result.status,
        accounts_mapped=result.accounts_mapped,
        transactions_attempted=result.transactions_attempted,
        transactions_exported=result.transactions_exported,
        transactions_failed=result.transactions_failed,
        transactions_skipped=result.transactions_skipped,
        holdings_exported=result.holdings_exported,
        csv_files=result.csv_files or [],
        duration_s=result.duration_s,
        error_message=result.error_message,
    )


@router.get("/runs", response_model=ExportRunsListResponse)
async def list_export_runs(
    _auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter runs by status. Use 'error' (alias for 'failed') to "
            "list dead-lettered / retryable runs with their error detail."
        ),
    ),
) -> ExportRunsListResponse:
    """List recent export runs.

    Pass ``?status=error`` (or ``?status=failed``) to list failed runs
    with their ``error_message`` so they can be inspected and retried
    via ``POST /exporters/{type}/runs/{id}/retry``.
    """
    # 'error' is the DLQ alias for the stored 'failed' status.
    if status_filter == "error":
        status_filter = "failed"

    filters: list[Any] = []
    if status_filter:
        filters.append(ExportRun.status == status_filter)

    # Total count
    count_stmt = select(ExportRun.id)
    if filters:
        count_stmt = count_stmt.where(*filters)
    count_result = await db.execute(count_stmt)
    total = len(count_result.all())

    # Fetch page
    stmt = (
        select(ExportRun)
        .order_by(ExportRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if filters:
        stmt = stmt.where(*filters)
    result = await db.execute(stmt)
    runs = list(result.scalars().all())

    return ExportRunsListResponse(
        runs=[
            ExportRunResponse(
                id=str(r.id),
                exporter_type=r.exporter_type,
                status=r.status,
                started_at=r.started_at,
                completed_at=r.completed_at,
                transactions_attempted=r.transactions_attempted,
                transactions_exported=r.transactions_exported,
                transactions_failed=r.transactions_failed,
                error_message=r.error_message,
            )
            for r in runs
        ],
        total=total,
    )


@router.get("/runs/{run_id}", response_model=ExportRunResponse)
async def get_export_run(
    run_id: str,
    _auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ExportRunResponse:
    """Get details of a specific export run."""
    run_uuid = _parse_run_id(run_id)
    if run_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export run not found",
        )
    result = await db.execute(select(ExportRun).where(ExportRun.id == run_uuid))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export run not found",
        )

    return ExportRunResponse(
        id=str(run.id),
        exporter_type=run.exporter_type,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        transactions_attempted=run.transactions_attempted,
        transactions_exported=run.transactions_exported,
        transactions_failed=run.transactions_failed,
        error_message=run.error_message,
    )


# ── Retry (dead-letter queue) ───────────────────────────────────────────


@router.post(
    "/{exporter_type}/runs/{run_id}/retry",
    response_model=RetryExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_export_run(
    exporter_type: str,
    run_id: str,
    request: Request,
    _auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> RetryExportResponse:
    """Retry a failed export run.

    Re-runs the exporter's export cycle (the same cycle
    ``POST /exporters/export`` triggers) without data loss: Actual
    Budget resumes from its per-account delivery cursor, and the
    Wealthfolio push path resumes from its ``wealthfolio_deliveries``
    cursor, so already-delivered transactions are not re-exported or
    duplicated.  A new ``ExportRun`` is created for the retry (reported
    as ``run_id``); the original failed run is kept for audit.
    """
    # Resolve + validate the exporter type
    container = get_container(request)
    settings = container.settings

    if exporter_type == "wealthfolio":
        if not settings.exporter_wealthfolio_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Wealthfolio exporter is disabled."
                    " Set EXPORTER_WEALTHFOLIO_ENABLED=true to enable."
                ),
            )
    elif exporter_type == "actual-budget":
        if not settings.exporter_actual_budget_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Actual Budget exporter is disabled."
                    " Set EXPORTER_ACTUAL_BUDGET_ENABLED=true to enable."
                ),
            )
    elif exporter_type == "firefly":
        if not settings.exporter_firefly_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Firefly exporter is disabled.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown exporter type: {exporter_type!r}",
        )

    # Load the failed run
    run_uuid = _parse_run_id(run_id)
    if run_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export run not found",
        )
    result = await db.execute(select(ExportRun).where(ExportRun.id == run_uuid))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export run not found",
        )
    if run.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed export runs can be retried (run is {run.status!r})",
        )
    if run.exporter_type is not None and run.exporter_type != exporter_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Export run {run_id} belongs to exporter "
                f"{run.exporter_type!r}, not {exporter_type!r}"
            ),
        )

    # Re-run the export cycle for this exporter type
    if exporter_type == "wealthfolio":
        wf_config = _build_wealthfolio_config(container)
        exporter = WealthfolioExporter(
            session_factory=container.session_factory,
            wf_config=wf_config,
            tenant_id=_auth.tenant_id,
        )
        outcome: Any = await exporter.run_export()
    elif exporter_type == "actual-budget":
        ab_config = ActualBudgetConfig.from_settings(settings)
        exporter = ActualBudgetExporter(
            session_factory=container.session_factory,
            ab_config=ab_config,
            tenant_id=_auth.tenant_id,
        )
        outcome = await exporter.run_export()
    else:
        outcome = await FireflyExporter(
            session_factory=container.session_factory,
            firefly_config=FireflyConfig.from_settings(settings),
            tenant_id=_auth.tenant_id,
        ).run_export()

    # The retry created a fresh ExportRun — report it from the result
    # (both exporter result types carry ``run_id``) instead of guessing
    # via a global "newest run" lookup, which can pick an unrelated run
    # under concurrency (worker sweep, parallel retries, other tenants).
    # Fall back only when the exporter had no run to report.
    retried_run_id = getattr(outcome, "run_id", None)
    if retried_run_id is None:
        newest_result = await db.execute(
            select(ExportRun).order_by(ExportRun.started_at.desc()).limit(1)
        )
        newest_run = newest_result.scalar_one_or_none()
        retried_run_id = (
            str(newest_run.id) if newest_run is not None else run_id
        )

    return RetryExportResponse(
        run_id=str(retried_run_id),
        status=outcome.status,
        transactions_attempted=outcome.transactions_attempted,
        transactions_exported=outcome.transactions_exported,
        transactions_failed=outcome.transactions_failed,
        error_message=outcome.error_message,
        duration_s=outcome.duration_s,
    )
