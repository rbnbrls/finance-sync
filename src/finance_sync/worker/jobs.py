"""Scheduled job implementations for the worker process.

Each job is an async function that performs a specific task — syncing a
connector, enriching prices, processing the outbox, or reconciling data.
Jobs accept the DI container and optionally a job monitor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from sqlalchemy import func, select

from finance_sync.config.settings import secret_value
from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.db.uow import UnitOfWork
from finance_sync.models.credential import (
    CONNECTION_STATUS_PAUSED,
    Credential,
)
from finance_sync.models.import_run import ImportRun
from finance_sync.services.degiro_import import (
    batch_hash,
    build_preview,
    connector_options,
    execute_run,
    validate_local_files,
)
from finance_sync.services.retry_lock import retry_lease
from finance_sync.sync.orchestrator import SyncOrchestrator
from finance_sync.sync.outbox_publisher import OutboxPublisher

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from finance_sync.config.settings import Settings
    from finance_sync.container import Container
    from finance_sync.models import Tenant

logger = structlog.get_logger("finance_sync.worker.jobs")


# ── Retry helper ──────────────────────────────────────────────────────


async def retry_with_backoff(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    job_name: str = "unknown",
) -> Any:
    """Execute *coro_factory* with exponential backoff retry.

    The factory is called once per attempt to produce a fresh coroutine.
    Raises the last exception after all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "job_retrying",
                    job=job_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_s=delay,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)
    # All attempts exhausted
    msg = f"Job {job_name!r} failed after {max_attempts} attempts"
    raise JobRetryError(msg) from last_exc


class JobRetryError(Exception):
    """Raised when all retry attempts for a job are exhausted."""


def retryable_job(
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Callable[..., Any]:
    """Decorator that adds retry logic to an async job function.

    Usage::

        @retryable_job(max_attempts=3, base_delay=1.0)
        async def my_job(container: Container) -> dict[str, Any]:
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await retry_with_backoff(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                job_name=func.__name__,
            )

        return wrapper

    return decorator


# ── Connector credential loading ──────────────────────────────────────


async def _get_tenant_connections(
    uow: UnitOfWork,
    provider_key: str,
) -> list[dict[str, Any]]:
    """Load *every* connection for *provider_key* across all tenants.

    Returns one entry per credential row (per connection), each with the
    owning tenant, the decrypted ``ConnectorConfig`` and the credential
    secrets (for error sanitisation).  A tenant may hold several
    connections for the same provider; each is synced independently.
    """
    tenants = await uow.tenants.list(limit=100)
    result: list[dict[str, Any]] = []

    for tenant in tenants:
        stmt = select(Credential).where(
            Credential.tenant_id == tenant.id,
            Credential.provider_key == provider_key,
        )
        cred_rows = (await uow.session.execute(stmt)).scalars().all()

        for cred in cred_rows:
            credentials: dict[str, str] = {}
            secrets: list[str] = []
            if cred.encrypted_payload:
                from finance_sync.services.auth import decrypt_credential

                try:
                    decrypted = decrypt_credential(
                        cred.encrypted_payload,
                        cred.nonce,
                        uow.session.info.get("settings"),
                    )
                    parsed: dict[str, Any] = json.loads(decrypted)
                    credentials = {str(k): str(v) for k, v in parsed.items()}
                    secrets = [str(v) for v in credentials.values()]
                except Exception:
                    logger.error(
                        "credential_decrypt_failed",
                        tenant_id=tenant.id,
                        provider_key=provider_key,
                        connection_id=str(cred.id),
                    )
                    continue
            config = ConnectorConfig(
                provider_type=provider_key,
                credentials=credentials,
                options=connector_options(cred),
                connection_id=str(cred.id),
                selected_accounts=list(cred.selected_accounts or []),
            )
            result.append(
                {
                    "tenant": tenant,
                    "credential": cred,
                    "config": config,
                    "secrets": secrets,
                }
            )

    return result


# ── Individual job functions ──────────────────────────────────────────


async def sync_connector_job(
    container: Container,
    provider_key: str,
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
) -> dict[str, Any]:
    """Sync a connector for all configured connections (per-tenant).

    Every active connection of *provider_key* is synced independently:

    - paused connections are skipped entirely (scheduler respects the
      connection's pause state; a manual sync can still be triggered)
    - a failing connection is reported and the loop continues with the
      remaining connections (errors never block siblings)
    - each connection resumes from its own stored sync cursor (per
      account); first syncs and accounts without a cursor fall back to
      the orchestrator's 90-day default window
    - the orchestrator records ``last_attempt_at`` / ``last_success_at``
      / sanitised ``last_error`` on each connection row

    Returns a summary dict with per-connection results.
    """
    settings: Settings = container.settings
    max_attempts = max_attempts or settings.worker_retry_max_attempts
    base_delay = base_delay or settings.worker_retry_base_delay_s

    registry = ConnectorRegistry()
    log = logger.bind(provider=provider_key)
    log.info("sync_job_starting")

    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        # Attach settings so credential decryption can access them
        session.info["settings"] = settings

        connections = await _get_tenant_connections(uow, provider_key)

    if not connections:
        log.info("sync_job_no_connections")
        return {
            "provider": provider_key,
            "connections_synced": 0,
            "results": [],
        }

    summary: list[dict[str, Any]] = []

    for conn in connections:
        tenant = conn["tenant"]
        cred = conn["credential"]
        config = conn["config"]
        connection_id = str(cred.id)
        conn_log = log.bind(
            tenant_id=tenant.id,
            connection_id=connection_id,
            connection_status=cred.status or "active",
        )

        # Paused connections are skipped by the scheduler (manual sync
        # remains possible via the REST API).  Skip quietly, keep the
        # summary entry so operators see the skip.
        if (cred.status or "active") == CONNECTION_STATUS_PAUSED:
            conn_log.info("sync_job_connection_skipped_paused")
            summary.append(
                {
                    "tenant_id": tenant.id,
                    "connection_id": connection_id,
                    "status": "skipped",
                    "reason": "paused",
                }
            )
            continue

        async def _run_single(
            _cfg: ConnectorConfig = config,
            _tenant: Tenant = tenant,
            _connection_id: str = connection_id,
            _selected: list[str] | None = cred.selected_accounts,
        ) -> dict[str, Any]:
            orchestrator = SyncOrchestrator(
                session_factory=container.session_factory,
                registry=registry,
                tenant_id=str(_tenant.id),
                settings=container.settings,
            )
            result = await orchestrator.run_sync(
                provider_type=_cfg.provider_type,
                config=_cfg,
                connection_id=_connection_id,
                selected_accounts=_selected,
            )
            return {
                "tenant_id": _tenant.id,
                "connection_id": _connection_id,
                "status": result.status.value,
                "accounts_synced": result.accounts_synced,
                "transactions_synced": result.transactions_synced,
                "holdings_synced": result.holdings_synced,
                "unresolved_securities": result.unresolved_securities,
                "duration_s": round(result.duration_s, 2),
                "error": result.error_message,
            }

        try:
            tenant_result = await retry_with_backoff(
                _run_single,
                max_attempts=max_attempts,
                base_delay=base_delay,
                job_name=f"sync_{provider_key}_{connection_id[:8]}",
            )
            conn_log.info(
                "sync_job_connection_complete",
                **tenant_result,
            )

            # Note: auto-reconciliation is handled inside run_sync() in
            # the orchestrator — no need to run it again here.
        except Exception as exc:
            tenant_result = {
                "tenant_id": tenant.id,
                "connection_id": connection_id,
                "status": "failed",
                "accounts_synced": 0,
                "transactions_synced": 0,
                "holdings_synced": 0,
                "unresolved_securities": 0,
                "duration_s": 0.0,
                "error": str(exc)[:500],
            }
            conn_log.error(
                "sync_job_connection_failed",
                error=str(exc)[:300],
            )

        summary.append(tenant_result)

    total_accounts = sum(r.get("accounts_synced", 0) for r in summary)
    total_transactions = sum(r.get("transactions_synced", 0) for r in summary)
    total_holdings = sum(r.get("holdings_synced", 0) for r in summary)
    total_unresolved = sum(r.get("unresolved_securities", 0) for r in summary)
    failed = [r for r in summary if r.get("status") == "failed"]
    skipped = [r for r in summary if r.get("status") == "skipped"]

    log.info(
        "sync_job_complete",
        connections_synced=len(summary),
        total_accounts=total_accounts,
        total_transactions=total_transactions,
        total_holdings=total_holdings,
        total_unresolved_securities=total_unresolved,
        failed=len(failed),
        skipped=len(skipped),
    )

    return {
        "provider": provider_key,
        "connections_synced": len(summary),
        "total_accounts": total_accounts,
        "total_transactions": total_transactions,
        "total_holdings": total_holdings,
        "total_unresolved_securities": total_unresolved,
        "failed": len(failed),
        "skipped": len(skipped),
        "results": summary,
    }


async def sync_bunq_job(container: Container) -> dict[str, Any]:
    """Sync all bunq connectors."""
    return await sync_connector_job(container, "bunq")


async def sync_bunq_cards_job(container: Container) -> dict[str, Any]:
    """Sync bunq card transactions + scheduled payments for all connections.

    Runs on an hourly cadence (see ``worker_job_bunq_cards_enabled`` /
    ``worker_job_bunq_cards_interval_hours``), independent of the
    15-minute transaction sync, matching the ARCHITECTURE.md §5 promise
    of hourly bunq cards/scheduled payments ingestion.

    Every active bunq connection is synced independently: paused
    connections are skipped and a failing connection never blocks the
    remaining ones.  The orchestrator records the per-connection
    ``last_attempt_at`` / ``last_success_at`` / sanitised ``last_error``.
    """
    settings: Settings = container.settings
    registry = ConnectorRegistry()
    log = logger.bind(provider="bunq_cards")
    log.info("bunq_cards_job_starting")

    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        session.info["settings"] = settings

        connections = await _get_tenant_connections(uow, "bunq")

    if not connections:
        log.info("bunq_cards_job_no_connections")
        return {
            "provider": "bunq_cards",
            "connections_synced": 0,
            "results": [],
        }

    summary: list[dict[str, Any]] = []

    for conn in connections:
        tenant = conn["tenant"]
        cred = conn["credential"]
        config = conn["config"]
        connection_id = str(cred.id)
        conn_log = log.bind(
            tenant_id=tenant.id,
            connection_id=connection_id,
            connection_status=cred.status or "active",
        )

        if (cred.status or "active") == CONNECTION_STATUS_PAUSED:
            conn_log.info("bunq_cards_job_connection_skipped_paused")
            summary.append(
                {
                    "tenant_id": tenant.id,
                    "connection_id": connection_id,
                    "status": "skipped",
                    "reason": "paused",
                }
            )
            continue

        async def _run_single(
            _cfg: ConnectorConfig = config,
            _tenant: Tenant = tenant,
            _connection_id: str = connection_id,
            _selected: list[str] | None = cred.selected_accounts,
        ) -> dict[str, Any]:
            orchestrator = SyncOrchestrator(
                session_factory=container.session_factory,
                registry=registry,
                tenant_id=str(_tenant.id),
                settings=settings,
            )
            result = await orchestrator.run_bunq_cards_sync(
                config=_cfg,
                connection_id=_connection_id,
                selected_accounts=_selected,
            )
            return {
                "tenant_id": _tenant.id,
                "connection_id": _connection_id,
                "status": result.status.value,
                "schedules_synced": result.schedules_synced,
                "card_transactions_synced": result.card_transactions_synced,
                "duration_s": round(result.duration_s, 2),
                "error": result.error_message,
            }

        try:
            tenant_result = await retry_with_backoff(
                _run_single,
                max_attempts=settings.worker_retry_max_attempts,
                base_delay=settings.worker_retry_base_delay_s,
                job_name=f"bunq_cards_{connection_id[:8]}",
            )
            conn_log.info("bunq_cards_job_connection_complete", **tenant_result)
        except Exception as exc:
            tenant_result = {
                "tenant_id": tenant.id,
                "connection_id": connection_id,
                "status": "failed",
                "schedules_synced": 0,
                "card_transactions_synced": 0,
                "duration_s": 0.0,
                "error": str(exc)[:500],
            }
            conn_log.error(
                "bunq_cards_job_connection_failed",
                error=str(exc)[:300],
            )

        summary.append(tenant_result)

    total_schedules = sum(r.get("schedules_synced", 0) for r in summary)
    total_cards = sum(r.get("card_transactions_synced", 0) for r in summary)
    failed = [r for r in summary if r.get("status") == "failed"]
    skipped = [r for r in summary if r.get("status") == "skipped"]

    log.info(
        "bunq_cards_job_complete",
        connections_synced=len(summary),
        total_schedules=total_schedules,
        total_card_transactions=total_cards,
        failed=len(failed),
        skipped=len(skipped),
    )

    return {
        "provider": "bunq_cards",
        "connections_synced": len(summary),
        "total_schedules": total_schedules,
        "total_card_transactions": total_cards,
        "failed": len(failed),
        "skipped": len(skipped),
        "results": summary,
    }


async def sync_trading212_job(container: Container) -> dict[str, Any]:
    """Sync all Trading212 connectors."""
    return await sync_connector_job(container, "trading212")


async def process_degiro_watchfolders_job(
    container: Container,
) -> dict[str, Any]:
    """Claim and atomically import stable files from configured watchfolders."""
    settings: Settings = container.settings
    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        session.info["settings"] = settings
        connections = await _get_tenant_connections(uow, "degiro_pension")

    processed = 0
    quarantined = 0
    duplicates = 0
    for conn in connections:
        tenant = conn["tenant"]
        cred = conn["credential"]
        config = conn["config"]
        connection_id = str(cred.id)
        # Paused connections are skipped by the scheduler, mirroring the
        # transaction/cards sync jobs.
        if (cred.status or "active") == CONNECTION_STATUS_PAUSED:
            logger.info(
                "degiro_watch_connection_skipped_paused",
                tenant_id=tenant.id,
                connection_id=connection_id,
            )
            continue
        watch = config.options.get("watchfolder")
        if not watch:
            continue
        incoming = Path(str(watch)).expanduser().resolve()  # noqa: ASYNC240
        incoming.mkdir(parents=True, exist_ok=True)
        now = time.time()
        candidates = [
            path
            for path in sorted(incoming.iterdir())
            if path.is_file()
            and path.suffix.lower() in {".csv", ".xlsx", ".xls"}
            and now - path.stat().st_mtime
            >= settings.degiro_watch_stable_seconds
        ]
        if not candidates:
            continue
        run_id = str(uuid4())
        claim_dir = incoming / ".processing" / run_id
        claim_dir.mkdir(parents=True, mode=0o700)
        claimed: list[Path] = []
        for candidate in candidates:
            try:
                target = claim_dir / candidate.name
                candidate.rename(target)
                claimed.append(target)
            except FileNotFoundError:
                continue
        if not claimed:
            shutil.rmtree(claim_dir, ignore_errors=True)
            continue

        archive = (
            Path(  # noqa: ASYNC240
                str(
                    config.options.get("archive_directory")
                    or incoming / "archive"
                )
            )
            .expanduser()
            .resolve()
        )
        quarantine = (
            Path(  # noqa: ASYNC240
                str(
                    config.options.get("quarantine_directory")
                    or incoming / "quarantine"
                )
            )
            .expanduser()
            .resolve()
        )
        hashes = [_file_sha256(path) for path in claimed]
        digest = batch_hash(hashes)
        async with container.session_factory() as session:
            # Use the loop's connection row (already loaded + decrypted);
            # re-querying here would break with multiple connections.
            prior = (
                await session.execute(
                    select(ImportRun).where(
                        ImportRun.tenant_id == tenant.id,
                        ImportRun.connection_id == connection_id,
                        ImportRun.batch_hash == digest,
                        ImportRun.status == "completed",
                    )
                )
            ).scalar_one_or_none()
            attempt = (
                int(
                    (
                        await session.execute(
                            select(func.max(ImportRun.attempt)).where(
                                ImportRun.tenant_id == tenant.id,
                                ImportRun.connection_id == connection_id,
                                ImportRun.batch_hash == digest,
                            )
                        )
                    ).scalar_one_or_none()
                    or 0
                )
                + 1
            )
            if prior is not None:
                duplicates += 1
                _move_batch(claimed, archive / run_id)
                session.add(
                    ImportRun(
                        id=run_id,
                        tenant_id=tenant.id,
                        connection_id=connection_id,
                        source="watchfolder",
                        status="duplicate",
                        batch_hash=digest,
                        attempt=attempt,
                        report_types=[],
                        content_hashes=hashes,
                        file_names=[path.name for path in claimed],
                        storage_names=[],
                        rows_total=0,
                        skipped_count=0,
                        warnings=["Identieke bestandsinhoud was al verwerkt."],
                        error_details=[],
                        preview={},
                        audit_events=[
                            {
                                "action": "duplicate_archived",
                                "at": datetime.now(UTC).isoformat(),
                            }
                        ],
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                shutil.rmtree(claim_dir, ignore_errors=True)
                continue
            try:
                validate_local_files(claimed, settings)
                preview = await build_preview(
                    claimed,
                    options=config.options,
                    settings=settings,
                    session=session,
                    tenant_id=str(tenant.id),
                )
                run = ImportRun(
                    id=run_id,
                    tenant_id=tenant.id,
                    connection_id=connection_id,
                    source="watchfolder",
                    status="previewed",
                    batch_hash=digest,
                    attempt=attempt,
                    report_types=preview["report_types"],
                    content_hashes=hashes,
                    file_names=[path.name for path in claimed],
                    storage_names=[path.name for path in claimed],
                    period_start=datetime.fromisoformat(preview["period_start"])
                    if preview.get("period_start")
                    else None,
                    period_end=datetime.fromisoformat(preview["period_end"])
                    if preview.get("period_end")
                    else None,
                    rows_total=preview["rows"],
                    skipped_count=preview["skipped"],
                    warnings=preview["warnings"],
                    error_details=[],
                    preview=preview,
                    audit_events=[
                        {
                            "action": "watchfolder_claimed",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    ],
                )
                session.add(run)
                await session.flush()
                await execute_run(
                    run,
                    paths=claimed,
                    options=config.options,
                    container=container,
                    session=session,
                    cleanup=False,
                )
                try:
                    _move_batch(claimed, archive / run_id)
                except OSError:
                    run.warnings = [
                        *run.warnings,
                        "Import voltooid; archivering vereist beheeractie.",
                    ]
                processed += 1
            except Exception as exc:
                await session.rollback()
                await _record_watch_failure(
                    session,
                    run_id=run_id,
                    tenant_id=str(tenant.id),
                    connection_id=connection_id,
                    digest=digest,
                    hashes=hashes,
                    files=claimed,
                    attempt=attempt,
                )
                _move_batch(claimed, quarantine / run_id)
                quarantined += 1
                logger.warning(
                    "degiro_watch_batch_quarantined",
                    tenant_id=str(tenant.id),
                    file_count=len(claimed),
                    error_type=type(exc).__name__,
                )
            await session.commit()
        shutil.rmtree(claim_dir, ignore_errors=True)
    return {
        "processed": processed,
        "quarantined": quarantined,
        "duplicates": duplicates,
    }


def _move_batch(paths: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    for index, path in enumerate(paths):
        if not path.exists():
            continue
        target = (
            destination / f"{index:02d}-{uuid4().hex[:8]}{path.suffix.lower()}"
        )
        path.rename(target)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _record_watch_failure(
    session: Any,
    *,
    run_id: str,
    tenant_id: str,
    connection_id: str,
    digest: str,
    hashes: list[str],
    files: list[Path],
    attempt: int,
) -> None:
    session.add(
        ImportRun(
            id=run_id,
            tenant_id=tenant_id,
            connection_id=connection_id,
            source="watchfolder",
            status="quarantined",
            batch_hash=digest,
            attempt=attempt,
            report_types=[],
            content_hashes=hashes,
            file_names=[path.name for path in files],
            storage_names=[],
            rows_total=0,
            rejected_count=0,
            warnings=[],
            error_details=[
                "Bestandsvalidatie mislukt; batch is in quarantine geplaatst."
            ],
            preview={},
            audit_events=[
                {"action": "quarantined", "at": datetime.now(UTC).isoformat()}
            ],
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def enrich_prices_job(container: Container) -> dict[str, Any]:
    """Enrich security prices for all securities that need fresh data.

    Runs every 15 minutes during market hours (9:30-16:00 EST).  Fetches
    latest quotes for all tracked securities and stores them as price
    observations.
    """
    log = logger.bind()
    log.info("enrich_prices_job_starting")
    gateway = container.enrichment_gateway
    async with container.session_factory() as session:
        from finance_sync.db.uow import UnitOfWork as _UoW

        uow = _UoW(session)
        securities = await uow.securities.list(limit=200)

        enriched = 0
        failed = 0
        for security in securities:
            # Determine the best identifier to use for the quote lookup
            identifier: str | None = None
            id_type: str = "ticker"
            if security.ticker:
                identifier = security.ticker
            elif security.figi:
                identifier = security.figi
                id_type = "figi"
            elif security.isin:
                identifier = security.isin
                id_type = "isin"

            if not identifier:
                continue

            try:
                quote = await gateway.get_latest_quote(
                    security_id=str(security.id),
                    identifier=identifier,
                    identifier_type=id_type,
                )
                if quote is not None:
                    enriched += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                log.debug(
                    "enrich_quote_failed",
                    security_id=str(security.id),
                    identifier=identifier,
                )

        await session.commit()

    log.info(
        "enrich_prices_job_complete",
        enriched=enriched,
        failed=failed,
    )
    return {"enriched": enriched, "failed": failed}


async def nightly_reconciliation_job(container: Container) -> dict[str, Any]:
    """Nightly full reconciliation: re-sync all connectors for all tenants
    and run reconciliation analysis on the fresh data.

    This is a heavy job that re-fetches all transactions from all
    configured connectors, reconciles them against the local data store,
    and emits reconciliation results via the outbox.
    """
    log = logger.bind()
    log.info("reconciliation_job_starting")

    results: list[dict[str, Any]] = []

    # ── Phase 1: Re-sync all connectors ─────────────────────────────
    try:
        bunq_result = await sync_bunq_job(container)
        results.append(bunq_result)
    except Exception as exc:
        results.append({"provider": "bunq", "error": str(exc)[:300]})
        log.error("reconciliation_bunq_failed", error=str(exc)[:200])

    try:
        t212_result = await sync_trading212_job(container)
        results.append(t212_result)
    except Exception as exc:
        results.append({"provider": "trading212", "error": str(exc)[:300]})
        log.error("reconciliation_t212_failed", error=str(exc)[:200])

    # ── Phase 2: Run reconciliation analysis per tenant ─────────────
    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        session.info["settings"] = container.settings

        tenants = await uow.tenants.list(limit=100)
        log.info("reconciliation_phase2_starting", tenant_count=len(tenants))

        for tenant in tenants:
            tenant_log = log.bind(tenant_id=tenant.id)
            try:
                orchestrator = SyncOrchestrator(
                    session_factory=container.session_factory,
                    registry=ConnectorRegistry(),
                    tenant_id=tenant.id,
                    settings=container.settings,
                )
                summary = await orchestrator.run_reconciliation()
                tenant_log.info(
                    "reconciliation_tenant_complete",
                    run_id=summary.run_id,
                    status=summary.status.value,
                    findings=summary.finding_count,
                )
                results.append(
                    {
                        "tenant_id": tenant.id,
                        "reconciliation_run_id": summary.run_id,
                        "status": summary.status.value,
                        "findings": summary.finding_count,
                    }
                )
            except Exception as exc:
                tenant_log.error(
                    "reconciliation_tenant_failed",
                    error=str(exc)[:300],
                )
                results.append(
                    {
                        "tenant_id": tenant.id,
                        "error": str(exc)[:300],
                    }
                )

    # ── Phase 3: Prune old price data during nightly reconciliation
    try:
        async with container.session_factory() as session:
            from finance_sync.enrichment.price_store import PriceStore

            store = PriceStore(
                session=session,
                settings=container.settings,
            )
            pruned_minute = await store.prune_intraday_data()
            pruned_hour = await store.prune_hourly_data()
            await session.commit()
            log.info(
                "reconciliation_pruning_complete",
                pruned_minute=pruned_minute,
                pruned_hour=pruned_hour,
            )
            results.append(
                {
                    "pruned_minute_prices": pruned_minute,
                    "pruned_hourly_prices": pruned_hour,
                },
            )
    except Exception as exc:
        log.error("reconciliation_pruning_failed", error=str(exc)[:200])

    log.info("reconciliation_job_complete")
    return {"status": "completed", "results": results}


async def process_webhook_retries_job(container: Container) -> dict[str, Any]:
    """Retry failed webhook deliveries whose retry time has arrived.

    Runs periodically alongside the outbox consumer.
    """
    from finance_sync.services.webhook import WebhookService

    svc = WebhookService(
        session_factory=container.session_factory,
        settings=container.settings,
        redis_client=(
            container.redis_client
            if container.settings.redis_url is not None
            else None
        ),
    )
    try:
        retried = await svc.retry_due_deliveries()
        logger.info("webhook_retry_job_complete", retried=retried)
        return {"retried": retried}
    except Exception:
        tb = traceback.format_exc()
        logger.error("webhook_retry_job_failed", error=tb[:500])
        raise
    finally:
        await svc.close()


async def process_outbox_job(container: Container) -> dict[str, Any]:
    """Process pending outbox messages.

    Runs every 30 seconds.  Dispatches pending outbox messages to
    registered handlers including webhooks.
    """
    log = logger.bind()
    publisher = OutboxPublisher(
        session_factory=container.session_factory,
        poll_interval=5.0,
        batch_size=50,
    )

    # Register webhook handler (catch-all — it filters internally)
    from finance_sync.services.webhook import WebhookService

    webhook_svc = WebhookService(
        session_factory=container.session_factory,
        settings=container.settings,
        redis_client=(
            container.redis_client
            if container.settings.redis_url is not None
            else None
        ),
    )
    publisher.register_handler("*", webhook_svc.handle_outbox_message)

    try:
        processed = await publisher.run_once()
        log.info("outbox_job_complete", processed=processed)
        return {"processed": processed}
    except Exception:
        tb = traceback.format_exc()
        log.error("outbox_job_failed", error=tb[:500])
        raise
    finally:
        await webhook_svc.close()


# ── Wealthfolio delivery sweep ─────────────────────────────────────────


async def export_wealthfolio_job(container: Container) -> dict[str, Any]:
    """Wealthfolio delivery sweep: push pending transactions for all
    tenants to the configured Wealthfolio instance.

    Runs on a 5-minute cadence (ARCHITECTURE.md §5: exporter delivery is
    on-demand — REST API / CLI — plus a 5-minute sweep).  The sweep is
    idempotent across worker restarts: ``push_to_wealthfolio`` resumes
    from the per-account ``wealthfolio_deliveries`` delivery cursor
    (G-14), so already-delivered transactions are never re-pushed.

    Skips cleanly when the sweep is disabled (``WORKER_JOB_EXPORT_ENABLED``
    false) or the Wealthfolio push target is not configured
    (``WEALTHFOLIO_SERVER_URL`` / ``WEALTHFOLIO_PASSWORD`` unset) — no
    crash, log + skip.  A per-tenant push failure is logged and recorded
    in the result summary; the sweep continues with the remaining tenants
    and the next tick retries the failed ones from their cursors.
    """
    settings: Settings = container.settings
    log = logger.bind()

    # ── Gating: flag + push target must both be present ────────────
    if not settings.worker_job_export_enabled:
        log.info("export_job_skipped_disabled")
        return {
            "status": "skipped",
            "reason": "WORKER_JOB_EXPORT_ENABLED=false",
        }

    server_url = settings.wealthfolio_server_url
    password = secret_value(settings.wealthfolio_password)
    if not server_url or not password:
        log.info(
            "export_job_skipped_unconfigured",
            has_server_url=bool(server_url),
            has_password=bool(password),
        )
        return {
            "status": "skipped",
            "reason": "WEALTHFOLIO_SERVER_URL/WEALTHFOLIO_PASSWORD not set",
        }

    # ── Load configured tenants ─────────────────────────────────────
    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        session.info["settings"] = settings
        tenants = await uow.tenants.list(limit=100)

    if not tenants:
        log.info("export_job_no_tenants")
        return {"status": "completed", "tenants": 0, "results": []}

    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    wf_config = WealthfolioConfig.from_settings(settings)
    wf_client = WealthfolioClient(
        config=WealthfolioClientConfig(
            base_url=server_url,
            password=password,
            request_timeout=settings.wealthfolio_request_timeout,
        ),
    )

    summary: list[dict[str, Any]] = []
    try:
        await wf_client.authenticate()
        for tenant in tenants:
            tenant_log = log.bind(tenant_id=tenant.id)
            try:
                lease = (
                    retry_lease(
                        container.redis_client,
                        tenant_id=str(tenant.id),
                        kind="destination-export",
                        item_id="legacy",
                    )
                    if settings.redis_url is not None
                    else None
                )
                if lease is not None:
                    async with lease:
                        if not lease.acquired:
                            tenant_log.info("export_job_tenant_skipped_overlap")
                            summary.append(
                                {
                                    "tenant_id": tenant.id,
                                    "status": "skipped",
                                    "reason": "export_in_progress",
                                }
                            )
                            continue
                        exporter = WealthfolioExporter(
                            session_factory=container.session_factory,
                            wf_config=wf_config,
                            tenant_id=tenant.id,
                        )
                        result = await exporter.push_to_wealthfolio(
                            wf_client=wf_client,
                        )
                else:
                    exporter = WealthfolioExporter(
                        session_factory=container.session_factory,
                        wf_config=wf_config,
                        tenant_id=tenant.id,
                    )
                    result = await exporter.push_to_wealthfolio(
                        wf_client=wf_client,
                    )
                tenant_status = (
                    "failed" if result.get("errors") else "completed"
                )
                tenant_log.info(
                    "export_job_tenant_complete",
                    status=tenant_status,
                    imported=result.get("imported", 0),
                    skipped=result.get("skipped", 0),
                    failed=result.get("failed", 0),
                    run_id=result.get("run_id"),
                )
                summary.append(
                    {
                        "tenant_id": tenant.id,
                        "status": tenant_status,
                        "imported": result.get("imported", 0),
                        "skipped": result.get("skipped", 0),
                        "failed": result.get("failed", 0),
                        "run_id": result.get("run_id"),
                    },
                )
            except Exception as exc:
                tenant_log.error(
                    "export_job_tenant_failed",
                    error=str(exc)[:300],
                )
                summary.append(
                    {
                        "tenant_id": tenant.id,
                        "status": "failed",
                        "error": str(exc)[:500],
                    },
                )
    finally:
        await wf_client.close()

    failed = [r for r in summary if r["status"] == "failed"]
    log.info(
        "export_job_complete",
        tenants=len(summary),
        failed=len(failed),
    )
    return {
        "status": "completed",
        "tenants": len(summary),
        "failed": len(failed),
        "results": summary,
    }


async def intel_refresh_job(container: Container) -> dict[str, Any]:
    """Refresh the market-intelligence source layer for all tenants.

    Runs on its own cadence (WORKER_JOB_INTEL_INTERVAL_MINUTES, default
    60m), independent of the bunq/Trading212/Wealthfolio sync jobs: a
    provider outage is isolated per provider (bounded timeout) and can
    never block the other syncs.  Each provider is only refreshed when
    its own freshness policy is due; failures are recorded in the
    provider-state table with sanitised errors (never credentials).
    """
    from finance_sync.intel.scheduler import intel_refresh_job as _run

    return await _run(container)


async def holding_relevance_build_job(container: Container) -> dict[str, Any]:
    """Build the holding-relevance feed for every tenant.

    Matches stored market-intelligence observations (already resolved
    to a canonical security by the intel layer) against each tenant's
    current and recently-sold holdings, then (re)clusters them into
    ranked stories.  Idempotent: re-running never duplicates rows, so a
    missed tick is harmless and a concurrent run is safe (unique
    constraints absorb the race).

    After each tenant's build, opt-in notifications are dispatched for
    newly created clusters (deduplicated per user/cluster/event type —
    never one notification per syndicated article).
    """

    from finance_sync.db.uow import UnitOfWork
    from finance_sync.services.holding_relevance import (
        HoldingRelevanceService,
    )

    logger.info("holding_relevance_build_starting")
    results: list[dict[str, Any]] = []
    async with container.session_factory() as session:
        uow = UnitOfWork(session)
        tenants = await uow.tenants.list(limit=1000)
        # Optional Hermes explanations (feature-flagged; off by default).
        # The build is deterministic either way — the explainer only
        # annotates the read DTOs, never the stored rows.
        if container.settings.hermes_explanation_enabled:
            from finance_sync.services.hermes_relevance import (
                build_hermes_explainer,
            )

            explainer = build_hermes_explainer(enabled=True)
        else:
            explainer = None
        for tenant in tenants:
            try:
                svc = HoldingRelevanceService(uow, explainer=explainer)
                summary = await svc.build_feed(str(tenant.id))
                # Opt-in notifications for new clusters/events
                # (deduplicated per user/cluster/event type).
                notifications = await svc.dispatch_new_cluster_notifications(
                    str(tenant.id)
                )
                await uow.commit()
                results.append(
                    {
                        "tenant_id": str(tenant.id),
                        "summary": summary,
                        "notifications": notifications,
                    }
                )
            except Exception as exc:
                logger.error(
                    "holding_relevance_build_failed",
                    tenant_id=str(tenant.id),
                    error=str(exc)[:300],
                )
                await uow.rollback()
                results.append(
                    {
                        "tenant_id": str(tenant.id),
                        "error": str(exc)[:300],
                    }
                )
    logger.info(
        "holding_relevance_build_complete",
        tenants=len(results),
        failed=sum(1 for r in results if "error" in r),
    )
    return {"status": "completed", "tenants": len(results), "results": results}
