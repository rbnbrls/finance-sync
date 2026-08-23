"""Common file-upload discovery and import helpers for the GUI."""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zipfile import ZipFile

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.dependencies import get_container, get_db
from finance_sync.models.credential import Credential
from finance_sync.models.import_run import ImportRun
from finance_sync.services.degiro_import import (
    ImportValidationError,
    connector_options,
    stage_uploads,
)
from finance_sync.sync.orchestrator import SyncOrchestrator

router = APIRouter(prefix="/connectors/file-uploads", tags=["file-uploads"])
_FILE_PROVIDERS = {
    "degiro_pension",
    "saxo_investor",
    "csv_import",
    "manual_expense",
}
_DISPLAY_NAMES = {
    "degiro_pension": "DEGIRO Pensioen",
    "saxo_investor": "SaxoInvestor",
    "csv_import": "CSV import",
    "manual_expense": "Handmatige uitgaven",
}


@router.get("/runs")
async def list_file_upload_runs(
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, object]]:
    rows = (
        (
            await db.execute(
                select(ImportRun)
                .where(
                    ImportRun.tenant_id == auth.tenant_id,
                    ImportRun.source == "upload",
                )
                .order_by(ImportRun.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at,
            "file_names": list(row.file_names),
            "status": row.status,
            "created_count": row.created_count,
            "updated_count": row.updated_count,
            "provider_type": "degiro_pension",
        }
        for row in rows
    ]


def _normalise(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _inspect_path(path: Path) -> tuple[set[str], list[str]]:
    """Return normalized content markers and human-readable evidence."""
    suffix = path.suffix.casefold()
    markers: set[str] = set()
    evidence: list[str] = []
    name = _normalise(path.name)
    if "degiro" in name or "portfolio" in name or "rekeningoverzicht" in name:
        markers.add("degiro_filename")
    if "saxo" in name or "posities" in name:
        markers.add("saxo_filename")

    if suffix in {".csv", ".txt"}:
        sample = path.read_text(encoding="utf-8-sig", errors="replace")[:16384]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))[:12]
        headers = {_normalise(cell) for row in rows[:4] for cell in row}
        degiro_markers = {
            "orderid",
            "orderidd",
            "valuedate",
            "valutadatum",
            "localvalue",
            "waardeineur",
            "transactionandorthirdpartyfees",
            "transactiekosten",
        }
        if len(headers & degiro_markers) >= 2:
            markers.add("degiro_content")
            evidence.append("DEGIRO-kolommen gevonden")
        if {"date", "datum", "amount", "bedrag"} & headers:
            markers.add("csv_content")
            evidence.append("datum- en bedragkolommen gevonden")
    elif suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        data_dict = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if isinstance(data_dict.get("expenses"), list):
            markers.add("manual_expense_content")
            evidence.append("expenses-lijst gevonden")
    elif suffix == ".xlsx":
        try:
            with ZipFile(path) as archive:
                xml = (
                    b" ".join(
                        archive.read(entry)
                        for entry in archive.namelist()
                        if (
                            entry.startswith("xl/worksheets/")
                            or entry == "xl/sharedStrings.xml"
                        )
                        and entry.endswith(".xml")
                    )[:500_000]
                    .decode("utf-8", errors="ignore")
                    .casefold()
                )
            for marker in (
                "isin",
                "quantity",
                "aantal",
                "closingprice",
                "slotkoers",
            ):
                if marker in xml:
                    markers.add("broker_xlsx_content")
            if "isin" in xml and (
                "quantity" in xml
                or "aantal" in xml
                or (
                    "transactiedatum" in xml
                    and ("boekingsbedrag" in xml or "transactie-id" in xml)
                )
            ):
                markers.add("saxo_content")
                evidence.append("Saxo-posities- of transactiekolommen gevonden")
            if "localvalue" in xml or "waardeineur" in xml:
                markers.add("degiro_content")
                evidence.append("DEGIRO-portefeuillekolommen gevonden")
        except (OSError, ValueError):
            pass
    return markers, evidence


def _detect(markers: set[str]) -> tuple[str | None, str]:
    if "saxo_content" in markers or "saxo_filename" in markers:
        return "saxo_investor", "SaxoInvestor-kenmerken gevonden"
    if "degiro_content" in markers or "degiro_filename" in markers:
        return "degiro_pension", "DEGIRO-kenmerken gevonden"
    if "manual_expense_content" in markers:
        return "manual_expense", "Een expenses-JSON-bestand gevonden"
    if "csv_content" in markers:
        return "csv_import", "Een algemeen CSV-transactieformaat gevonden"
    return (
        None,
        "De broker kon niet betrouwbaar uit de bestanden worden afgeleid",
    )


@router.post("/inspect")
async def inspect_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    auth: AuthContext = Depends(require_permission("connectors", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Inspect uploads without importing or retaining their contents."""
    settings = get_container(request).settings
    run_id = str(uuid4())
    staged: list[Path] = []
    try:
        staged, names, _ = await stage_uploads(
            files,
            settings=settings,
            tenant_id=auth.tenant_id,
            run_id=run_id,
            check_formulas=False,
        )
        markers: set[str] = set()
        evidence: list[str] = []
        for path in staged:
            path_markers, path_evidence = _inspect_path(path)
            markers.update(path_markers)
            evidence.extend(path_evidence)
        provider, reason = _detect(markers)
        rows = (
            (
                await db.execute(
                    select(Credential).where(
                        Credential.tenant_id == auth.tenant_id,
                        Credential.provider_key.in_(_FILE_PROVIDERS),
                    )
                )
            )
            .scalars()
            .all()
        )
        flows = [
            {
                "provider_type": row.provider_key,
                "display_name": _DISPLAY_NAMES.get(
                    row.provider_key, row.provider_key
                ),
                "connection_id": str(row.id),
                "account_name": connector_options(row).get("account_name"),
                "ready": True,
            }
            for row in rows
        ]
        configured = {flow["provider_type"] for flow in flows}
        flows.extend(
            {
                "provider_type": provider_name,
                "display_name": display_name,
                "connection_id": None,
                "account_name": "Nog niet ingesteld",
                "ready": False,
            }
            for provider_name, display_name in _DISPLAY_NAMES.items()
            if provider_name not in configured
        )
        return {
            "file_names": names,
            "detected_provider": provider,
            "confidence": "high" if provider else "unknown",
            "reason": reason,
            "evidence": sorted(set(evidence)),
            "flows": flows,
        }
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if staged:
            shutil.rmtree(staged[0].parent, ignore_errors=True)


@router.post("/import")
async def import_generic_file(
    request: Request,
    connection_id: str,
    files: list[UploadFile] = File(...),
    auth: AuthContext = Depends(require_permission("connectors", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Import CSV/manual JSON files through their normal connector."""
    row = await db.scalar(
        select(Credential).where(
            Credential.id == connection_id,
            Credential.tenant_id == auth.tenant_id,
        )
    )
    if row is None or row.provider_key not in {"csv_import", "manual_expense"}:
        raise HTTPException(
            status_code=404, detail="File-importconfiguratie niet gevonden."
        )
    settings = get_container(request).settings
    run_id = str(uuid4())
    staged: list[Path] = []
    try:
        staged, names, _ = await stage_uploads(
            files,
            settings=settings,
            tenant_id=auth.tenant_id,
            run_id=run_id,
            check_formulas=False,
        )
        options = connector_options(row)
        if row.provider_key == "csv_import":
            options = {**options, "csv_path": str(staged[0])}
            if "column_mapping" not in options:
                options["column_mapping"] = _csv_mapping(staged[0])
        else:
            if staged[0].suffix.casefold() != ".json":
                raise HTTPException(
                    status_code=422,
                    detail="Handmatige uitgaven verwachten een JSON-bestand.",
                )
            options = {**options, "data_path": str(staged[0])}
        config = ConnectorConfig(
            provider_type=row.provider_key, options=options
        )
        result = await SyncOrchestrator(
            session_factory=get_container(request).session_factory,
            registry=ConnectorRegistry(),
            tenant_id=auth.tenant_id,
            settings=settings,
        ).run_sync(
            provider_type=row.provider_key,
            config=config,
            since=datetime.min.replace(tzinfo=UTC),
            connection_id=str(row.id),
        )
        await db.commit()
        return {
            "status": result.status.value,
            "file_names": names,
            "message": "Bestand geïmporteerd.",
        }
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)[:500]) from exc
    finally:
        if staged:
            shutil.rmtree(staged[0].parent, ignore_errors=True)


def _csv_mapping(path: Path) -> dict[str, str]:
    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:8192]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    headers = list(
        next(csv.reader(io.StringIO(sample), delimiter=delimiter), [""])
    )
    normalized = {_normalise(header): header for header in headers}

    def pick(*names: str) -> str | None:
        return next(
            (normalized[name] for name in names if name in normalized), None
        )

    mapping: dict[str, str] = {}
    for key, names in {
        "date": ("date", "datum", "transactiondate", "boekdatum"),
        "description": ("description", "omschrijving", "name", "naam"),
        "amount": ("amount", "bedrag", "value", "waarde"),
    }.items():
        value = pick(*names)
        if value:
            mapping[key] = value
    return mapping
