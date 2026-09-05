"""APScheduler setup — persistent job store, job registration, lifecycle.

Uses ``AsyncIOScheduler`` with a ``SQLAlchemyJobStore`` backed by the
same PostgreSQL database as the application, so scheduled jobs survive
worker restarts.
"""

# APScheduler does not publish type stubs; keep its third-party imports from
# consuming the project's strict Pyright warning budget.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import traceback
from typing import TYPE_CHECKING, Any, cast

import structlog
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from finance_sync.worker.jobs import (
    data_quality_repair_job,
    enrich_prices_job,
    export_wealthfolio_job,
    holding_relevance_build_job,
    intel_refresh_job,
    nightly_reconciliation_job,
    process_degiro_watchfolders_job,
    process_outbox_job,
    process_webhook_retries_job,
    sync_bunq_cards_job,
    sync_bunq_job,
    sync_trading212_job,
)
from finance_sync.worker.monitoring import JobRunContext
from finance_sync.worker.schedule_runner import run_scheduled_syncs_job

if TYPE_CHECKING:
    from finance_sync.config.settings import Settings
    from finance_sync.container import Container
    from finance_sync.worker.monitoring import JobMonitor

logger = structlog.get_logger("finance_sync.worker.scheduler")


# ── Market-hours helper ───────────────────────────────────────────────


def _market_hours_cron(
    settings: Settings,
    *,
    minute_interval: int = 15,
) -> CronTrigger:
    """Build a CronTrigger that fires every *minute_interval* minutes
    during US market hours (9:30-16:00 EST).

    EST = UTC - 5 (standard time) or UTC - 4 (daylight saving).
    We cover the widest window by computing the UTC equivalent using
    a generous cushion (opens at 09:00 EST, closes at 16:30 EST).
    """
    open_str = settings.worker_job_price_enrichment_market_open  # "09:30"
    close_str = settings.worker_job_price_enrichment_market_close  # "16:00"

    open_h, _open_m = (int(x) for x in open_str.split(":"))
    close_h, _close_m = (int(x) for x in close_str.split(":"))

    # Convert EST → UTC (add 5h for standard time).  During DST this
    # fires slightly early and stays slightly late, which is fine —
    # the job is a no-op when the market is closed anyway.
    utc_open_h = (open_h + 5) % 24
    utc_close_h = (close_h + 5) % 24

    return CronTrigger(
        minute=f"*/{minute_interval}",
        hour=f"{utc_open_h}-{utc_close_h}",
        day_of_week="mon-fri",
        timezone="UTC",
    )


# ── Job-store URL helper ───────────────────────────────────────────────


def sync_jobstore_url(engine_url: str | None) -> str | None:
    """Return a synchronous-driver URL for the APScheduler job store.

    The application's ``DATABASE_URL`` uses the asyncpg driver
    (``postgresql+asyncpg://``) because the FastAPI app is async.  APScheduler's
    ``SQLAlchemyJobStore`` is synchronous and cannot use an async-only driver —
    with the asyncpg DSN it crashes at scheduler start with
    ``MissingGreenlet`` (verified on the production worker, 2026-08-16).  Map
    the async driver to the sync psycopg (v3) driver; any other URL (or None)
    passes through unchanged.
    """
    if not engine_url:
        return None
    async_driver = "postgresql+asyncpg://"
    if engine_url.startswith(async_driver):
        return "postgresql+psycopg://" + engine_url[len(async_driver) :]
    return engine_url


# ── Scheduler wrapper ─────────────────────────────────────────────────


# Module-level active scheduler registry.
#
# APScheduler's SQLAlchemyJobStore pickles job callables into PostgreSQL.
# Closures over ``self`` and bound methods of WorkerScheduler are not
# picklable ("This Job cannot be serialized since the reference to its
# callable could not be determined" — observed on the production worker,
# 2026-08-16).  Monitored jobs are therefore registered as a module-level
# function with a stable ``job_id`` argument; the entrypoint resolves the
# owning scheduler from module state at run time.  Persisting ``id(self)`` and
# a function object in the job arguments made jobs restored after a worker
# restart point at a scheduler instance that no longer existed.  The job then
# appeared to execute successfully while doing no work.
_schedulers: dict[int, WorkerScheduler] = {}
_active_scheduler: WorkerScheduler | None = None


async def _monitored_job_entrypoint(
    job_id_or_scheduler_key: str | int,
    legacy_job_id: str | None = None,
    legacy_func: Any | None = None,
) -> None:
    """Run a persisted job on the currently active worker scheduler.

    The optional legacy arguments keep direct callers and jobs persisted by
    older builds compatible during one rolling restart.  Newly registered
    jobs persist only their stable job id.
    """
    global _active_scheduler
    if legacy_job_id is not None:
        # Compatibility with the pre-fix persisted shape.
        scheduler = _schedulers.get(int(job_id_or_scheduler_key))
        job_id = legacy_job_id
        func = legacy_func
    else:
        scheduler = _active_scheduler
        job_id = str(job_id_or_scheduler_key)
        func = scheduler.job_function(job_id) if scheduler else None
    if scheduler is None:
        logger.error(
            "job_dropped_no_active_scheduler",
            job_id=job_id,
        )
        return
    if func is None:
        logger.error("job_dropped_unknown_job", job_id=job_id)
        return
    await scheduler.run_monitored_job(job_id, func)


class WorkerScheduler:
    """Wraps APScheduler with finance-sync specific setup and monitoring.

    Usage::

        scheduler = WorkerScheduler(settings, container, monitor)
        await scheduler.start()
        ...
        await scheduler.stop()
    """

    def __init__(
        self,
        settings: Settings,
        container: Container,
        monitor: JobMonitor,
    ) -> None:
        self._settings = settings
        self._container = container
        self._monitor = monitor
        self._scheduler = self._build_scheduler()
        self._job_ids: list[str] = []
        self._job_functions: dict[str, Any] = {}
        self._running_jobs: set[str] = set()

    # ── Public API ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler and register all configured jobs."""
        # Load the persistent job store while paused.  Registration must see
        # already-persisted jobs so legacy records can be removed before any
        # overdue job is allowed to fire.
        self._scheduler.start(paused=True)
        self._register_jobs()
        self._scheduler.resume()
        logger.info(
            "scheduler_started",
            jobs=self.job_summary(),
        )

    async def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("scheduler_stopped")
        _schedulers.pop(id(self), None)
        global _active_scheduler
        if _active_scheduler is self:
            _active_scheduler = None

    def pause(self) -> None:
        """Pause the scheduler — no new jobs fire."""
        if self._scheduler.running:
            self._scheduler.pause()
            logger.info("scheduler_paused")

    def resume(self) -> None:
        """Resume a paused scheduler."""
        self._scheduler.resume()
        logger.info("scheduler_resumed")

    def is_running(self) -> bool:
        """Return True if the APScheduler is currently running."""
        return self._scheduler.running

    def running_jobs(self) -> list[str]:
        """Return list of currently executing job IDs."""
        return list(self._running_jobs)

    def job_function(self, job_id: str) -> Any | None:
        """Resolve a registered monitored job for the persistence stub."""
        return self._job_functions.get(job_id)

    async def wait_for_completion(self) -> None:
        """Wait for all currently running jobs to complete."""
        while self._running_jobs:
            logger.debug(
                "scheduler_waiting_for_jobs",
                running=list(self._running_jobs),
            )
            await asyncio.sleep(0.5)

    def job_summary(self) -> list[dict[str, Any]]:
        """Return a summary of registered jobs."""
        summary: list[dict[str, Any]] = []
        for job in cast("Any", self._scheduler).get_jobs():
            trigger_desc = str(job.trigger)
            summary.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": (
                        job.next_run_time.isoformat()
                        if job.next_run_time
                        else None
                    ),
                    "trigger": trigger_desc,
                }
            )
        return summary

    # ── Internal ─────────────────────────────────────────────────────

    def _build_scheduler(self) -> AsyncIOScheduler:
        """Create and configure the APScheduler instance."""
        engine_url = (
            self._settings.database_url.unicode_string()
            if self._settings.database_url
            else None
        )

        jobstores: dict[str, Any] = {}
        if engine_url:
            # SQLAlchemyJobStore is synchronous; never feed it the asyncpg
            # DSN (MissingGreenlet at scheduler start — see sync_jobstore_url).
            jobstores["default"] = SQLAlchemyJobStore(
                url=sync_jobstore_url(engine_url),
                engine_options={
                    "pool_size": 2,
                    "max_overflow": 2,
                },
            )

        return AsyncIOScheduler(
            jobstores=jobstores,
            timezone="UTC",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )

    def _register_jobs(self) -> None:
        """Register all scheduled jobs based on settings."""
        settings = self._settings

        # A persistent APScheduler store can retain a job after its feature
        # flag is disabled.  Leaving that row in place makes the old
        # module-level entrypoint fire on every interval and report a false
        # ``job_dropped_no_active_scheduler`` error.  Remove the known
        # optional sweep before evaluating the current setting.
        if not settings.worker_job_export_enabled:
            try:
                if cast("Any", self._scheduler).get_job(
                    "export_wealthfolio"
                ) is not None:
                    cast("Any", self._scheduler).remove_job(
                        "export_wealthfolio"
                    )
                    logger.info(
                        "scheduled_job_removed_disabled",
                        job_id="export_wealthfolio",
                    )
            except Exception:
                # Scheduler startup must remain available even if a stale
                # persistent row cannot be inspected or removed.
                logger.warning(
                    "scheduled_job_cleanup_failed",
                    job_id="export_wealthfolio",
                    error=traceback.format_exc()[-1000:],
                )

        # ── Tenant schedule dispatch (minute tick) ──────────────────
        # The per-tenant sync_schedules drive *when* each connection /
        # export target runs.  A minute tick checks due schedules and
        # claims them atomically (idempotent across replicas/restarts).
        # Gated by WORKER_JOB_SCHEDULES_ENABLED so operators can disable
        # the whole tenant scheduling layer.
        if settings.worker_job_schedules_enabled:
            self._add_job(
                "run_scheduled_syncs",
                run_scheduled_syncs_job,
                trigger=IntervalTrigger(minutes=1),
            )

        # ── Market-intelligence refresh job ────────────────────────
        # Refreshes the intel providers (SEC EDGAR public data,
        # optionally OpenBB) on their own cadence.  Independent of the
        # bunq/Trading212/Wealthfolio sync jobs — a provider outage
        # never blocks those.  Gated by WORKER_JOB_INTEL_ENABLED.
        if settings.worker_job_intel_enabled:
            self._add_job(
                "intel_refresh",
                intel_refresh_job,
                trigger=IntervalTrigger(
                    minutes=settings.worker_job_intel_interval_minutes,
                ),
            )

        # ── Holding-relevance feed build job ───────────────────────
        # Matches stored intel observations to current/recently-sold
        # holdings and (re)clusters them into ranked stories on its own
        # cadence.  Idempotent — a missed tick is harmless and a
        # concurrent run is safe.  Gated by
        # WORKER_JOB_HOLDING_RELEVANCE_ENABLED.
        if settings.worker_job_holding_relevance_enabled:
            self._add_job(
                "holding_relevance_build",
                holding_relevance_build_job,
                trigger=IntervalTrigger(
                    minutes=settings.worker_job_holding_relevance_interval_minutes,
                ),
            )

        # ── bunq sync job ───────────────────────────────────────────
        if settings.worker_job_bunq_sync_enabled:
            self._add_job(
                "sync_bunq",
                sync_bunq_job,
                trigger=IntervalTrigger(
                    minutes=settings.worker_job_bunq_sync_interval_minutes,
                ),
            )

        # ── bunq cards/scheduled-payments job (hourly) ──────────────
        # Independent cadence from the main transaction sync, gated by
        # its own feature flag (dr.3).
        if settings.worker_job_bunq_cards_enabled:
            self._add_job(
                "sync_bunq_cards",
                sync_bunq_cards_job,
                trigger=IntervalTrigger(
                    hours=settings.worker_job_bunq_cards_interval_hours,
                ),
            )

        # ── Trading212 sync job ─────────────────────────────────────
        if settings.worker_job_trading212_sync_enabled:
            self._add_job(
                "sync_trading212",
                sync_trading212_job,
                trigger=IntervalTrigger(
                    hours=settings.worker_job_trading212_sync_interval_hours,
                ),
            )

        if settings.worker_job_degiro_watch_enabled:
            self._add_job(
                "process_degiro_watchfolders",
                process_degiro_watchfolders_job,
                trigger=IntervalTrigger(
                    seconds=settings.worker_job_degiro_watch_interval_seconds,
                ),
            )

        # ── Price enrichment job ────────────────────────────────────
        if settings.worker_job_price_enrichment_enabled:
            trigger = _market_hours_cron(
                settings,
                minute_interval=settings.worker_job_price_enrichment_interval_minutes,
            )
            self._add_job(
                "enrich_prices",
                enrich_prices_job,
                trigger=trigger,
            )

        if getattr(settings, "worker_job_data_quality_repair_enabled", False):
            self._add_job(
                "data_quality_repair",
                data_quality_repair_job,
                trigger=IntervalTrigger(
                    minutes=settings.worker_job_data_quality_repair_interval_minutes,
                ),
            )

        # ── Nightly reconciliation job ──────────────────────────────
        if settings.worker_job_reconciliation_enabled:
            cron_parts = settings.worker_job_reconciliation_cron.split()
            if len(cron_parts) == 5:
                self._add_job(
                    "nightly_reconciliation",
                    nightly_reconciliation_job,
                    trigger=CronTrigger(
                        minute=cron_parts[0],
                        hour=cron_parts[1],
                        day=cron_parts[2],
                        month=cron_parts[3],
                        day_of_week=cron_parts[4],
                        timezone="UTC",
                    ),
                )

        # ── Outbox consumer job ─────────────────────────────────────
        if settings.worker_job_outbox_enabled:
            self._add_job(
                "process_outbox",
                process_outbox_job,
                trigger=IntervalTrigger(
                    seconds=settings.worker_job_outbox_interval_seconds,
                ),
            )

        # ── Webhook retry job ─────────────────────────────────────
        if settings.worker_job_outbox_enabled:
            self._add_job(
                "process_webhook_retries",
                process_webhook_retries_job,
                trigger=IntervalTrigger(
                    seconds=settings.worker_job_outbox_interval_seconds,
                ),
            )

        # ── Wealthfolio delivery sweep job ────────────────────────
        # ARCHITECTURE.md §5: exporter delivery is on-demand (REST API /
        # CLI) plus a 5-minute sweep.  The sweep resumes from the G-14
        # delivery cursor, so it is idempotent across worker restarts.
        # Gated on WORKER_JOB_EXPORT_ENABLED (default: enabled only when
        # the
        # Wealthfolio push target env vars are set) — the job itself
        # also skips cleanly when the target is unconfigured.
        if settings.worker_job_export_enabled:
            self._add_job(
                "export_wealthfolio",
                export_wealthfolio_job,
                trigger=IntervalTrigger(
                    minutes=settings.worker_job_export_interval_minutes,
                ),
            )

    def _add_job(
        self,
        job_id: str,
        func: Any,
        *,
        trigger: Any,
    ) -> None:
        """Register a monitored job with APScheduler and track its ID.

        The callable registered is the module-level
        ``_monitored_job_entrypoint`` — APScheduler's ``Job.__getstate__``
        resolves job callables to ``module:qualname`` textual references for
        persistent stores, so closures, bound methods and ``functools.partial``
        all fail ("reference to its callable could not be determined").  The
        The job id is the only persisted argument; the active scheduler keeps
        the in-process function mapping.
        """
        global _active_scheduler
        _schedulers[id(self)] = self
        _active_scheduler = self
        self._job_functions[job_id] = func
        scheduler = cast("Any", self._scheduler)
        # APScheduler may keep the deserialised state of a legacy job when
        # replacing a record whose callable/args changed across releases.
        # Remove it first so the persisted entry is guaranteed to contain
        # only the current stable job id.
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
        scheduler.add_job(
            _monitored_job_entrypoint,
            trigger=trigger,
            args=[job_id],
            id=job_id,
            name=job_id.replace("_", " ").title(),
            replace_existing=True,
        )
        self._job_ids.append(job_id)

    async def run_monitored_job(
        self,
        job_id: str,
        func: Any,
    ) -> None:
        """Execute *func* under job monitoring and error logging.

        Invoked by the module-level entrypoint (which APScheduler
        deserialises from the job store); keeping the heavy body here
        avoids duplicating monitoring logic in the picklable stub.
        """
        self._running_jobs.add(job_id)
        try:
            async with JobRunContext(
                self._monitor,
                job_id,
                name=job_id.replace("_", " ").title(),
            ):
                await func(self._container)
        except Exception:
            logger.error(
                "job_unhandled_error",
                job_id=job_id,
                error=traceback.format_exc(),
            )
        finally:
            self._running_jobs.discard(job_id)
