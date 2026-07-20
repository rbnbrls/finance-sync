"""APScheduler-based background worker process for finance-sync.

The worker runs scheduled sync jobs (bunq, Trading212), price enrichment,
full reconciliation, and outbox message processing — all coordinated by
APScheduler with a persistent PostgreSQL job store.

Usage
-----
    python -m finance_sync.worker

Or via the Docker image with a different entrypoint (``worker`` CMD in
docker-compose.yml).
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog

from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.observability.logging import configure_logging
from finance_sync.worker.health import WorkerHealthServer
from finance_sync.worker.monitoring import JobMonitor
from finance_sync.worker.scheduler import WorkerScheduler

logger = structlog.get_logger("finance_sync.worker")


class WorkerProcess:
    """Top-level coordinator for the worker process.

    Owns the DI container, APScheduler instance, health server, and job
    monitor.  Handles startup, signal registration, and graceful shutdown.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._container: Container | None = None
        self._scheduler: WorkerScheduler | None = None
        self._health_server: WorkerHealthServer | None = None
        self._monitor: JobMonitor | None = None
        self._shutdown_event = asyncio.Event()
        self._running_tasks: set[asyncio.Task[Any]] = set()

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise infrastructure and start all components."""
        log = logger.bind(settings=self._settings.environment.value)
        log.info("worker_starting")

        # 1. DI container
        self._container = Container.from_settings(self._settings)

        async with self._container.dispose():
            try:
                # 2. Job monitor
                self._monitor = JobMonitor()

                # 3. APScheduler
                self._scheduler = WorkerScheduler(
                    settings=self._settings,
                    container=self._container,
                    monitor=self._monitor,
                )
                await self._scheduler.start()

                # 4. Health HTTP server (separate port)
                self._health_server = WorkerHealthServer(
                    port=self._settings.worker_health_port,
                    monitor=self._monitor,
                    scheduler=self._scheduler,
                )
                health_task = asyncio.create_task(
                    self._health_server.serve(),
                )
                self._running_tasks.add(health_task)
                health_task.add_done_callback(self._running_tasks.discard)

                # 5. Register OS signals for graceful shutdown
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(
                        sig,
                        lambda s=sig: self._signal_handler(s),
                    )

                log.info(
                    "worker_started",
                    jobs=self._scheduler.job_summary(),
                    health_port=self._settings.worker_health_port,
                )

                # 6. Wait for shutdown signal
                await self._shutdown_event.wait()

            finally:
                await self._shutdown()
                log.info("worker_stopped")

    async def _shutdown(self) -> None:
        """Graceful shutdown: drain jobs, stop servers, dispose infra."""
        log = logger.bind()
        log.info("worker_shutting_down")

        # 1. Pause scheduler — no new jobs
        if self._scheduler is not None:
            self._scheduler.pause()

        # 2. Cancel health server
        if self._health_server is not None:
            await self._health_server.stop()

        # 3. Wait for running jobs to finish (drain)
        if self._scheduler is not None:
            running = self._scheduler.running_jobs()
            if running:
                log.info("worker_draining_jobs", count=len(running))
                # Give running jobs up to 30s to complete
                try:
                    await asyncio.wait_for(
                        self._scheduler.wait_for_completion(),
                        timeout=30.0,
                    )
                except TimeoutError:
                    log.warning("worker_drain_timeout")

        # 4. Shut down APScheduler
        if self._scheduler is not None:
            await self._scheduler.stop()

        # 5. Cancel remaining tasks
        for task in self._running_tasks:
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(
                *self._running_tasks,
                return_exceptions=True,
            )

    def _signal_handler(self, sig: signal.Signals) -> None:
        """Handle OS termination signals."""
        logger.info(
            "worker_signal_received",
            signal=sig.name,
        )
        self._shutdown_event.set()


def run_worker(settings: Settings | None = None) -> None:
    """Run the worker process synchronously (blocking).

    This is the main entrypoint used by ``python -m finance_sync.worker``
    and the Docker ``CMD`` override.
    """
    if settings is None:
        settings = Settings()

    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    worker = WorkerProcess(settings)
    try:
        asyncio.run(worker.start())
    except KeyboardInterrupt:
        logger.info("worker_keyboard_interrupt")


def main() -> None:
    """CLI entrypoint."""
    run_worker()


if __name__ == "__main__":
    main()
