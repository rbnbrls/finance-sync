"""REST API for managing exporters and triggering export runs.

Allows authenticated users to view exporter configuration (read from
environment variables / Settings) and trigger export runs to Wealthfolio.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

if TYPE_CHECKING:
    from datetime import datetime
    from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, get_auth_context
from finance_sync.dependencies import get_container, get_db
from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
from finance_sync.exporter.wealthfolio.exporter import (
    WealthfolioExporter,
    WealthfolioExportResult,
)

router = APIRouter(prefix="/exporters", tags=["exporters"])

# ── Pydantic schemas ─────────────────────────────────────────────────────


class ExporterTypeInfo(BaseModel):
    """Public info about an available exporter type."""

    name: str = Field(description="Exporter key, e.g. 'wealthfolio'")
    display_name: str = Field(description="Human-readable name")
    description: str = Field(description="Brief description of the exporter")
    config_fields: list[dict[str, object]] = Field(
        default_factory=list,
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
    status: str
    started_at: datetime
    completed_at: datetime | None
    transactions_attempted: int | None
    transactions_exported: int | None
    transactions_failed: int | None
    error_message: str | None


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


# ── Helpers ──────────────────────────────────────────────────────────────


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
        export_holdings=getattr(
            settings, "wealthfolio_export_holdings", True
        ),
        max_transactions_per_file=getattr(
            settings, "wealthfolio_max_transactions_per_file", 10_000
        ),
        include_pending=getattr(
            settings, "wealthfolio_include_pending", False
        ),
        account_name_overrides=getattr(
            settings, "wealthfolio_account_name_overrides", {}
        ),
        instrument_type_overrides=getattr(
            settings, "wealthfolio_instrument_type_overrides", {}
        ),
    )


# ── Endpoints ───────────────────────────────────────────────────────────


@router.get("/types", response_model=list[ExporterTypeInfo])
async def list_exporter_types() -> list[ExporterTypeInfo]:
    """List all available exporter types with their metadata."""
    return [
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
                    "default": "/tmp/finance_sync_wealthfolio_exports",
                    "description": "Directory for generated CSV export files",
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
        ),
    ]


@router.get("/config", response_model=ExporterConfigResponse)
async def get_exporter_config(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),  # noqa: ARG001
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
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),  # noqa: ARG001
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
    auth: AuthContext = Depends(get_auth_context),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ExportRunsListResponse:
    """List recent export runs."""
    # Total count
    count_result = await db.execute(
        select(ExportRun.id)
    )
    total = len(count_result.all())

    # Fetch page
    stmt = (
        select(ExportRun)
        .order_by(ExportRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    runs = list(result.scalars().all())

    return ExportRunsListResponse(
        runs=[
            ExportRunResponse(
                id=r.id,
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
    auth: AuthContext = Depends(get_auth_context),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> ExportRunResponse:
    """Get details of a specific export run."""
    result = await db.execute(
        select(ExportRun).where(ExportRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export run not found",
        )

    return ExportRunResponse(
        id=run.id,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        transactions_attempted=run.transactions_attempted,
        transactions_exported=run.transactions_exported,
        transactions_failed=run.transactions_failed,
        error_message=run.error_message,
    )
