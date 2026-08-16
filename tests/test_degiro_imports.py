"""Security and workflow tests for DEGIRO upload/watchfolder imports."""

# pyright: basic

from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

from finance_sync.config.settings import Settings
from finance_sync.models.credential import Credential
from finance_sync.models.import_run import ImportRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.db.uow import UnitOfWork
from finance_sync.services.degiro_import import (
    ImportValidationError,
    build_preview,
    cleanup_expired_previews,
    connector_options,
    stage_uploads,
    verify_staged,
)

FIXTURES = Path(__file__).parent / "connectors/degiro_pension/fixtures"


def _settings(tmp_path: Path, **changes: Any) -> Settings:
    return Settings(
        debug=False,
        database_url=None,
        redis_url=None,
        degiro_import_staging_directory=tmp_path,
        **changes,
    )


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


@pytest.mark.asyncio
async def test_streams_valid_upload_and_builds_preview(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    content = (FIXTURES / "transactions_nl.csv").read_bytes()
    paths, names, hashes = await stage_uploads(
        [_upload("transactions.csv", content)],
        settings=settings,
        tenant_id="tenant-a",
        run_id="run-a",
    )
    assert names == ["transactions.csv"]
    assert hashes == [hashlib.sha256(content).hexdigest()]
    assert paths[0].stat().st_size == len(content)
    assert paths[0].parent.stat().st_mode & 0o777 == 0o700

    result = MagicMock()
    result.scalar_one.return_value = 0
    session = cast(
        "AsyncSession",
        SimpleNamespace(execute=AsyncMock(return_value=result)),
    )
    preview = await build_preview(
        paths,
        options={"account_key": "preview-test"},
        settings=settings,
        session=session,
        tenant_id="tenant-a",
    )
    assert preview["report_types"] == ["transactions"]
    assert preview["transactions"] == 3
    assert preview["missing_report_types"] == [
        "account_statement",
        "portfolio",
    ]


@pytest.mark.asyncio
async def test_rejects_path_traversal_and_cleans_staging(
    tmp_path: Path,
) -> None:
    with pytest.raises(ImportValidationError, match="ongeldig pad"):
        await stage_uploads(
            [_upload("../account.csv", b"Date,Description\n")],
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            run_id="run-b",
        )
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_enforces_streamed_size_limit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, degiro_import_max_file_bytes=1024)
    with pytest.raises(ImportValidationError, match="uploadlimiet"):
        await stage_uploads(
            [_upload("large.csv", b"x" * 1025)],
            settings=settings,
            tenant_id="tenant-a",
            run_id="run-c",
        )
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_rejects_formula_injection(tmp_path: Path) -> None:
    content = b"Datum,Tijd,Product,ISIN,Aantal,Koers,Totaal\n2026-01-01,12:00,=CMD(),IE00B4L5Y983,1,1,1\n"
    with pytest.raises(ImportValidationError, match="formule"):
        await stage_uploads(
            [_upload("formula.csv", content)],
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            run_id="run-d",
        )


@pytest.mark.asyncio
async def test_rejects_fake_xlsx_before_parser(tmp_path: Path) -> None:
    with pytest.raises(ImportValidationError, match="XLSX-bestand"):
        await stage_uploads(
            [_upload("fake.xlsx", b"not a zip")],
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            run_id="run-e",
        )


def test_confirmation_detects_toctou_change(tmp_path: Path) -> None:
    path = tmp_path / "01.csv"
    path.write_bytes(b"first")
    run = ImportRun(
        content_hashes=[hashlib.sha256(b"first").hexdigest()],
        storage_names=[path.name],
    )
    verify_staged(run, [path])
    path.write_bytes(b"second")
    with pytest.raises(ImportValidationError, match="gewijzigd"):
        verify_staged(run, [path])


def test_cleanup_only_removes_expired_preview_dirs(tmp_path: Path) -> None:
    settings = _settings(tmp_path, degiro_import_preview_ttl_minutes=10)
    old = tmp_path / "tenant" / "old"
    fresh = tmp_path / "tenant" / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    timestamp = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(old, (timestamp, timestamp))
    assert cleanup_expired_previews(settings) == 1
    assert not old.exists()
    assert fresh.exists()


def test_connector_options_load_without_credentials() -> None:
    credential = Credential(
        provider_key="degiro_pension",
        tenant_id="00000000-0000-0000-0000-000000000001",
        encrypted_payload=b"",
        nonce=b"",
        description=(
            '{"watchfolder":"/imports/degiro/incoming",'
            '"account_key":"pension-a","_label":"Pensioen"}'
        ),
    )
    assert connector_options(credential) == {
        "watchfolder": "/imports/degiro/incoming",
        "account_key": "pension-a",
    }


def test_openapi_exposes_upload_confirm_and_recovery() -> None:
    from finance_sync.app import create_app

    schema = create_app(settings=_settings(Path("/tmp/test-imports"))).openapi()
    paths = schema["paths"]
    prefix = "/api/v1/connectors/degiro-pension/imports"
    assert f"{prefix}/preview" in paths
    assert f"{prefix}/{{run_id}}/confirm" in paths
    assert f"{prefix}/{{run_id}}/retry" in paths
    request_body = paths[f"{prefix}/preview"]["post"]["requestBody"]
    assert "multipart/form-data" in request_body["content"]


@pytest.mark.asyncio
async def test_worker_loads_options_for_secretless_connector() -> None:
    from finance_sync.worker.jobs import _get_tenant_credentials

    tenant = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
    credential = Credential(
        provider_key="degiro_pension",
        tenant_id=tenant.id,
        encrypted_payload=b"",
        nonce=b"",
        description='{"watchfolder":"/imports/degiro/incoming"}',
    )
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = credential
    uow = cast(
        "UnitOfWork",
        SimpleNamespace(
            tenants=SimpleNamespace(list=AsyncMock(return_value=[tenant])),
            session=SimpleNamespace(
                execute=AsyncMock(return_value=scalar_result),
                info={"settings": _settings(Path("/tmp/test-imports"))},
            ),
        ),
    )
    configs = await _get_tenant_credentials(uow, "degiro_pension")
    assert len(configs) == 1
    assert configs[0][1].credentials == {}
    assert configs[0][1].options["watchfolder"] == "/imports/degiro/incoming"


@pytest.mark.asyncio
async def test_scheduler_registers_watchfolder_sweep(tmp_path: Path) -> None:
    from finance_sync.container import Container
    from finance_sync.worker.monitoring import JobMonitor
    from finance_sync.worker.scheduler import WorkerScheduler

    settings = _settings(
        tmp_path,
        worker_job_bunq_sync_enabled=False,
        worker_job_bunq_cards_enabled=False,
        worker_job_trading212_sync_enabled=False,
        worker_job_price_enrichment_enabled=False,
        worker_job_reconciliation_enabled=False,
        worker_job_outbox_enabled=False,
        worker_job_export_enabled=False,
        worker_job_degiro_watch_enabled=True,
        worker_job_degiro_watch_interval_seconds=37,
    )
    scheduler = WorkerScheduler(
        settings, Container.from_settings(settings), JobMonitor()
    )
    await scheduler.start()
    jobs = {job["id"]: job for job in scheduler.job_summary()}
    assert "process_degiro_watchfolders" in jobs
    assert "interval[0:00:37]" in jobs["process_degiro_watchfolders"]["trigger"]
    await scheduler.stop()
