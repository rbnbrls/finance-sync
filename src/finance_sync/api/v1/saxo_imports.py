"""Simple one-step XLSX upload for the SaxoInvestor connector."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.connectors.exceptions import PermanentError
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.connectors.saxo_investor import SaxoInvestorConnector
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import Credential
from finance_sync.models.import_run import ImportRun
from finance_sync.services.degiro_import import (
    batch_hash,
    connector_options,
    stage_uploads,
)
from finance_sync.sync.orchestrator import SyncOrchestrator

router = APIRouter(
    prefix="/connectors/saxo-investor/imports", tags=["saxo-imports"]
)


class SaxoImportResponse(BaseModel):
    status: str
    file_names: list[str]
    accounts: int
    transactions: int
    holdings: int
    unresolved_securities: int
    message: str


async def _connection(
    session: AsyncSession, connection_id: str, tenant_id: str
) -> Credential:
    connection = (
        await session.execute(
            select(Credential).where(
                Credential.id == connection_id,
                Credential.tenant_id == tenant_id,
                Credential.provider_key == "saxo_investor",
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SaxoInvestor-configuratie niet gevonden.",
        )
    return connection


@router.post(
    "", response_model=SaxoImportResponse, status_code=status.HTTP_200_OK
)
async def import_files(
    request: Request,
    connection_id: str = Form(...),
    files: list[UploadFile] = File(...),
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> SaxoImportResponse:
    """Import exactly one positions file and one transactions file."""
    connection = await _connection(db, connection_id, auth.tenant_id)
    container = get_container(request)
    run_id = str(uuid4())
    staged: list[Path] = []
    import_run: ImportRun | None = None
    try:
        staged, names, hashes = await stage_uploads(
            files,
            settings=container.settings,
            tenant_id=auth.tenant_id,
            run_id=run_id,
            check_formulas=False,
        )
        if any(path.suffix.casefold() != ".xlsx" for path in staged):
            message = "SaxoInvestor ondersteunt alleen XLSX-bestanden."
            raise PermanentError(message)
        if len(staged) != 2:
            message = (
                "Upload precies twee SaxoInvestor-bestanden: één Posities-"
                "bestand en één Transacties-bestand."
            )
            raise PermanentError(message)
        validator = SaxoInvestorConnector(
            ConnectorConfig(
                provider_type="saxo_investor",
                options={"export_paths": [str(path) for path in staged]},
            )
        )
        await validator.authenticate()
        if set(validator.export_roles) != {"positions", "transactions"}:
            message = (
                "Upload één herkenbaar Posities-bestand en één herkenbaar "
                "Transacties-bestand van SaxoInvestor."
            )
            raise PermanentError(message)
        previous_attempt = await db.scalar(
            select(func.max(ImportRun.attempt)).where(
                ImportRun.tenant_id == auth.tenant_id,
                ImportRun.connection_id == connection.id,
                ImportRun.batch_hash == batch_hash(hashes),
            )
        )
        import_run = ImportRun(
            id=run_id,
            tenant_id=auth.tenant_id,
            connection_id=connection.id,
            source="upload",
            status="running",
            batch_hash=batch_hash(hashes),
            # Re-uploading the same two files is a new auditable attempt, not
            # a duplicate-key failure. This is especially important for the
            # wizard: an operator must be able to retry after fixing a parser
            # or downstream sync error without changing the files.
            attempt=(previous_attempt or 0) + 1,
            report_types=["positions", "transactions"],
            content_hashes=hashes,
            file_names=names,
            storage_names=[path.name for path in staged],
            rows_total=0,
            warnings=[],
            error_details=[],
            preview={},
            audit_events=[],
        )
        db.add(import_run)
        await db.flush()
        options = connector_options(connection)
        config = ConnectorConfig(
            provider_type="saxo_investor",
            options={**options, "export_paths": [str(path) for path in staged]},
        )
        orchestrator = SyncOrchestrator(
            session_factory=container.session_factory,
            registry=ConnectorRegistry(),
            tenant_id=auth.tenant_id,
            settings=container.settings,
        )
        result = await orchestrator.run_sync(
            provider_type="saxo_investor",
            config=config,
            since=datetime.min.replace(tzinfo=UTC),
            connection_id=str(connection.id),
        )
        if result.status.value == "failed":
            import_run.status = "failed"
            import_run.error_details = [
                str(result.error_message or "Saxo-import mislukt.")[:500]
            ]
            import_run.completed_at = datetime.now(UTC)
            raise PermanentError(
                result.error_message or "De Saxo-import is mislukt."
            )
        import_run.status = result.status.value
        import_run.rows_total = (
            result.accounts_synced
            + result.transactions_synced
            + result.holdings_synced
        )
        import_run.created_count = import_run.rows_total
        import_run.completed_at = datetime.now(UTC)
        await db.commit()
        return SaxoImportResponse(
            status=result.status.value,
            file_names=names,
            accounts=result.accounts_synced,
            transactions=result.transactions_synced,
            holdings=result.holdings_synced,
            unresolved_securities=result.unresolved_securities,
            message="Saxo-bestanden zijn geïmporteerd.",
        )
    except PermanentError as exc:
        if import_run is not None:
            import_run.status = "failed"
            import_run.error_details = [str(exc)[:500]]
            import_run.completed_at = datetime.now(UTC)
            await db.commit()
        else:
            await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        if import_run is not None:
            import_run.status = "failed"
            import_run.error_details = [str(exc)[:500]]
            import_run.completed_at = datetime.now(UTC)
            await db.commit()
        else:
            await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="De Saxo-import is mislukt.",
        ) from exc
    finally:
        if staged:
            shutil.rmtree(staged[0].parent, ignore_errors=True)
