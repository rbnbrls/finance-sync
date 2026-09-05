"""Tenant-scoped upload and audit API for DEGIRO official exports."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import Credential
from finance_sync.models.import_run import ImportRun
from finance_sync.services.degiro_import import (
    ImportValidationError,
    batch_hash,
    build_preview,
    cleanup_expired_previews,
    connector_options,
    execute_run,
    stage_paths,
    stage_uploads,
)
from finance_sync.services.incident_reporting import report_connector_failure

router = APIRouter(
    prefix="/connectors/degiro-pension/imports",
    tags=["degiro-imports"],
)


class ImportRunResponse(BaseModel):
    id: str
    connection_id: str
    source: str
    status: str
    report_types: list[str]
    content_hashes: list[str]
    file_names: list[str]
    period_start: datetime | None
    period_end: datetime | None
    rows_total: int
    created_count: int
    updated_count: int
    skipped_count: int
    rejected_count: int
    warnings: list[str]
    error: str | None
    preview: dict[str, Any]
    retained: bool
    created_at: datetime
    completed_at: datetime | None


class ConfirmRequest(BaseModel):
    retain_encrypted: bool = Field(
        default=False,
        description="Keep encrypted originals after the confirmed import",
    )
    force_reimport: bool = Field(
        default=False,
        description="Admin override for an already completed content hash",
    )


def _response(run: ImportRun) -> ImportRunResponse:
    return ImportRunResponse(
        id=str(run.id),
        connection_id=str(run.connection_id),
        source=run.source,
        status=run.status,
        report_types=list(run.report_types),
        content_hashes=list(run.content_hashes),
        file_names=list(run.file_names),
        period_start=run.period_start,
        period_end=run.period_end,
        rows_total=run.rows_total,
        created_count=run.created_count,
        updated_count=run.updated_count,
        skipped_count=run.skipped_count,
        rejected_count=run.rejected_count,
        warnings=list(run.warnings),
        error=run.safe_error,
        preview=dict(run.preview),
        retained=run.retained,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _connection(
    session: AsyncSession, connection_id: str, tenant_id: str
) -> Credential:
    connection = (
        await session.execute(
            select(Credential).where(
                Credential.id == connection_id,
                Credential.tenant_id == tenant_id,
                Credential.provider_key == "degiro_pension",
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DEGIRO Pensioen-configuratie niet gevonden.",
        )
    return connection


@router.post(
    "/preview",
    response_model=ImportRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def preview_import(
    request: Request,
    connection_id: str = Form(...),
    files: list[UploadFile] = File(...),
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ImportRunResponse:
    """Stream, validate and preview files without writing financial data."""
    connection = await _connection(db, connection_id, auth.tenant_id)
    container = get_container(request)
    cleanup_expired_previews(container.settings)
    run_id = str(uuid4())
    staged: list[Path] = []
    try:
        staged, names, hashes = await stage_uploads(
            files,
            settings=container.settings,
            tenant_id=auth.tenant_id,
            run_id=run_id,
        )
        preview = await build_preview(
            staged,
            options=connector_options(connection),
            settings=container.settings,
            session=db,
            tenant_id=auth.tenant_id,
        )
        digest = batch_hash(hashes)
        prior = (
            await db.execute(
                select(ImportRun.id).where(
                    ImportRun.tenant_id == auth.tenant_id,
                    ImportRun.connection_id == connection.id,
                    ImportRun.batch_hash == digest,
                    ImportRun.status == "completed",
                )
            )
        ).first()
        previous_attempt = int(
            (
                await db.execute(
                    select(func.max(ImportRun.attempt)).where(
                        ImportRun.tenant_id == auth.tenant_id,
                        ImportRun.connection_id == connection.id,
                        ImportRun.batch_hash == digest,
                    )
                )
            ).scalar_one_or_none()
            or 0
        )
        preview["already_processed"] = prior is not None
        period_start = (
            datetime.fromisoformat(preview["period_start"])
            if preview.get("period_start")
            else None
        )
        period_end = (
            datetime.fromisoformat(preview["period_end"])
            if preview.get("period_end")
            else None
        )
        run = ImportRun(
            id=run_id,
            tenant_id=auth.tenant_id,
            connection_id=connection.id,
            source="upload",
            status="previewed",
            batch_hash=digest,
            attempt=previous_attempt + 1,
            report_types=preview["report_types"],
            content_hashes=hashes,
            file_names=names,
            storage_names=[path.name for path in staged],
            period_start=period_start,
            period_end=period_end,
            rows_total=preview["rows"],
            skipped_count=preview["skipped"],
            warnings=preview["warnings"],
            error_details=[],
            preview=preview,
            audit_events=[
                {
                    "action": "previewed",
                    "principal": auth.principal_id,
                    "at": datetime.now(UTC).isoformat(),
                }
            ],
        )
        db.add(run)
        await db.flush()
        return _response(run)
    except ImportValidationError as exc:
        if staged:
            import shutil

            shutil.rmtree(staged[0].parent, ignore_errors=True)
        await _record_failed_preview(db, run_id, auth, connection, str(exc))
        await db.commit()
        await report_connector_failure(
            container.settings,
            exc,
            connector="degiro_pension",
            operation="file_import_preview",
            connection_id=str(connection.id),
            correlation_id=run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except PermanentError as exc:
        if staged:
            import shutil

            shutil.rmtree(staged[0].parent, ignore_errors=True)
        detail = str(exc)[:500]
        await _record_failed_preview(db, run_id, auth, connection, detail)
        await db.commit()
        await report_connector_failure(
            container.settings,
            exc,
            connector="degiro_pension",
            operation="file_import_preview",
            connection_id=str(connection.id),
            correlation_id=run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        ) from exc
    except Exception as exc:
        if staged:
            import shutil

            shutil.rmtree(staged[0].parent, ignore_errors=True)
        detail = "De DEGIRO-export kon niet veilig worden gevalideerd."
        await _record_failed_preview(db, run_id, auth, connection, detail)
        await db.commit()
        await report_connector_failure(
            container.settings,
            exc,
            connector="degiro_pension",
            operation="file_import_preview",
            connection_id=str(connection.id),
            correlation_id=run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        ) from exc


@router.post("/{run_id}/confirm", response_model=ImportRunResponse)
async def confirm_import(
    run_id: str,
    body: ConfirmRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ImportRunResponse:
    """Confirm exactly the staged and hashed files from a prior preview."""
    # ``force_reimport`` re-ingests an already-completed content hash, so it
    # stays an explicit admin-only override even though the endpoint itself is
    # now open to every principal holding ``connectors:write``.
    if (
        body.force_reimport
        and auth.user is not None
        and auth.user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="force_reimport is restricted to admin users",
        )
    run = (
        await db.execute(
            select(ImportRun).where(
                ImportRun.id == run_id,
                ImportRun.tenant_id == auth.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=404, detail="Importpoging niet gevonden."
        )
    if run.status != "previewed":
        raise HTTPException(
            status_code=409, detail="Deze import kan niet worden bevestigd."
        )
    if run.preview.get("already_processed") and not body.force_reimport:
        raise HTTPException(
            status_code=409,
            detail="Deze bestanden zijn al succesvol verwerkt.",
        )
    connection = await _connection(db, str(run.connection_id), auth.tenant_id)
    container = get_container(request)
    paths = stage_paths(container.settings, auth.tenant_id, run)
    try:
        await execute_run(
            run,
            paths=paths,
            options=connector_options(connection),
            container=container,
            session=db,
            retain=body.retain_encrypted,
        )
        if run.status == "completed":
            connection.last_success_at = run.completed_at
            connection.last_attempt_at = run.completed_at
            connection.last_error = None
            connection.last_error_category = None
    except ImportValidationError as exc:
        await report_connector_failure(
            container.settings,
            exc,
            connector="degiro_pension",
            operation="file_import_confirm",
            connection_id=str(connection.id),
            correlation_id=run_id,
        )
        await db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        await report_connector_failure(
            container.settings,
            exc,
            connector="degiro_pension",
            operation="file_import_confirm",
            connection_id=str(connection.id),
            correlation_id=run_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=500,
            detail="De import is atomair teruggedraaid.",
        ) from exc
    return _response(run)


async def _record_failed_preview(
    db: AsyncSession,
    run_id: str,
    auth: AuthContext,
    connection: Credential,
    detail: str,
) -> None:
    digest = hashlib.sha256(run_id.encode()).hexdigest()
    run = ImportRun(
        id=run_id,
        tenant_id=auth.tenant_id,
        connection_id=connection.id,
        source="upload",
        status="failed",
        batch_hash=digest,
        attempt=1,
        report_types=[],
        content_hashes=[],
        file_names=[],
        storage_names=[],
        rows_total=0,
        rejected_count=0,
        warnings=[],
        error_details=[detail[:500]],
        preview={},
        audit_events=[
            {
                "action": "validation_failed",
                "principal": auth.principal_id,
                "at": datetime.now(UTC).isoformat(),
            }
        ],
        completed_at=datetime.now(UTC),
    )
    db.add(run)
    await db.flush()


@router.get("", response_model=list[ImportRunResponse])
async def list_import_runs(
    request: Request,
    connection_id: str | None = Query(default=None),
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[ImportRunResponse]:
    """List tenant-scoped status/freshness without exposing server paths."""
    cleanup_expired_previews(get_container(request).settings)
    query = select(ImportRun).where(ImportRun.tenant_id == auth.tenant_id)
    if connection_id:
        query = query.where(ImportRun.connection_id == connection_id)
    rows = (
        (
            await db.execute(
                query.order_by(ImportRun.created_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [_response(run) for run in rows]


@router.delete("/{run_id}/files", response_model=ImportRunResponse)
async def delete_import_files(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ImportRunResponse:
    """Explicitly remove staged/retained files while preserving the audit row."""
    run = (
        await db.execute(
            select(ImportRun).where(
                ImportRun.id == run_id,
                ImportRun.tenant_id == auth.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=404, detail="Importpoging niet gevonden."
        )
    import shutil

    container = get_container(request)
    for path in stage_paths(container.settings, auth.tenant_id, run):
        if path.parent.exists():
            shutil.rmtree(path.parent, ignore_errors=True)
            break
    retained = (
        container.settings.degiro_import_staging_directory
        / "retained"
        / str(run.id)
    )
    shutil.rmtree(retained, ignore_errors=True)
    connection = await _connection(db, str(run.connection_id), auth.tenant_id)
    options = connector_options(connection)
    watchfolder = options.get("watchfolder")
    if watchfolder:
        quarantine = Path(  # noqa: ASYNC240
            str(
                options.get("quarantine_directory")
                or Path(str(watchfolder)).expanduser()  # noqa: ASYNC240
                / "quarantine"
            )
        ).expanduser()
        shutil.rmtree(quarantine / str(run.id), ignore_errors=True)
    run.retained = False
    run.audit_events = [
        *run.audit_events,
        {
            "action": "files_deleted",
            "principal": auth.principal_id,
            "at": datetime.now(UTC).isoformat(),
        },
    ]
    await db.flush()
    return _response(run)


@router.post("/{run_id}/retry", response_model=ImportRunResponse)
async def retry_quarantined_import(
    run_id: str,
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> ImportRunResponse:
    """Queue a quarantined batch for one explicit, audited worker retry."""
    run = (
        await db.execute(
            select(ImportRun).where(
                ImportRun.id == run_id,
                ImportRun.tenant_id == auth.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=404, detail="Importpoging niet gevonden."
        )
    if run.status != "quarantined":
        raise HTTPException(
            status_code=409, detail="Alleen quarantine kan opnieuw."
        )
    connection = await _connection(db, str(run.connection_id), auth.tenant_id)
    options = connector_options(connection)
    watch_value = options.get("watchfolder")
    if not watch_value:
        raise HTTPException(
            status_code=409, detail="Geen watchfolder geconfigureerd."
        )
    watchfolder = Path(str(watch_value)).expanduser()  # noqa: ASYNC240
    quarantine = Path(  # noqa: ASYNC240
        str(options.get("quarantine_directory") or watchfolder / "quarantine")
    ).expanduser()
    source = quarantine / str(run.id)
    if not source.is_dir():
        raise HTTPException(
            status_code=410, detail="Quarantinebestanden ontbreken."
        )
    watchfolder.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            path.rename(
                watchfolder / f"retry-{uuid4().hex[:12]}{path.suffix.lower()}"
            )
    source.rmdir()
    run.status = "retry_queued"
    run.audit_events = [
        *run.audit_events,
        {
            "action": "retry_queued",
            "principal": auth.principal_id,
            "at": datetime.now(UTC).isoformat(),
        },
    ]
    await db.flush()
    return _response(run)
