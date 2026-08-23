"""Worker health HTTP server — separate port from the FastAPI application.

Provides health probes (liveness, readiness) and job status introspection
for the worker process.  Uses a minimal ``aiohttp`` web server.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from finance_sync.worker.monitoring import JobMonitor
    from finance_sync.worker.scheduler import WorkerScheduler

logger = structlog.get_logger("finance_sync.worker.health")

_START_TIME: float = time.time()


def _uptime() -> float:
    return round(time.time() - _START_TIME, 2)


# ── Minimal async HTTP handler ───────────────────────────────────────


class WorkerHealthServer:
    """Minimal HTTP health server for the worker process.

    Routes
    ------
    ``GET /health``         — overall worker health + job summary
    ``GET /health/live``    — liveness probe (always 200)
    ``GET /health/ready``   — readiness probe (scheduler running)
    ``GET /health/jobs``    — per-job run history from monitor

    Runs on ``WORKER_HEALTH_PORT`` (default 9090).
    """

    def __init__(
        self,
        port: int = 9090,
        monitor: JobMonitor | None = None,
        scheduler: WorkerScheduler | None = None,
    ) -> None:
        self._port = port
        self._monitor = monitor
        self._scheduler = scheduler
        self._server: Any = None
        self._shutdown_event = asyncio.Event()

    async def serve(self) -> None:
        """Start the health HTTP server (runs until cancelled)."""
        from aiohttp import web

        app = web.Application()

        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/health/", self._handle_health)
        app.router.add_get("/health/live", self._handle_live)
        app.router.add_get("/health/ready", self._handle_ready)
        app.router.add_get("/health/jobs", self._handle_jobs)

        runner = web.AppRunner(app)
        await runner.setup()
        self._server = web.TCPSite(runner, host="0.0.0.0", port=self._port)
        await self._server.start()

        logger.info(
            "health_server_started",
            port=self._port,
        )

        # Keep the task alive until stop() signals
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop the health server."""
        if self._server is not None:
            await self._server.stop()
            self._shutdown_event.set()
            logger.info("health_server_stopped")

    # ── Request handlers ─────────────────────────────────────────────

    async def _handle_health(
        self,
        _request: Any,  # web.Request
    ) -> Any:  # web.Response
        """Overall health check — worker status + scheduler summary."""
        scheduler_running = self._is_scheduler_running()

        body: dict[str, Any] = {
            "status": "ok" if scheduler_running else "degraded",
            "uptime": _uptime(),
            "scheduler": {
                "running": scheduler_running,
            },
        }

        if self._scheduler is not None:
            body["scheduler"]["jobs"] = self._scheduler.job_summary()

        return self._json_response(body)

    async def _handle_live(
        self,
        _request: Any,  # web.Request
    ) -> Any:  # web.Response
        """Liveness probe — process is alive."""
        return self._json_response({"status": "ok"})

    async def _handle_ready(
        self,
        _request: Any,  # web.Request
    ) -> Any:  # web.Response
        """Readiness probe — scheduler is running."""
        scheduler_running = self._is_scheduler_running()
        return self._json_response(
            {
                "status": "ok" if scheduler_running else "not_ready",
                "scheduler_running": scheduler_running,
            }
        )

    async def _handle_jobs(
        self,
        _request: Any,  # web.Request
    ) -> Any:  # web.Response
        """Per-job run history from the JobMonitor."""
        if self._monitor is None:
            return self._json_response({"jobs": []})
        return self._json_response(
            {
                "jobs": self._monitor.summarize(),
            }
        )

    # ── Internal helpers ─────────────────────────────────────────────

    def _is_scheduler_running(self) -> bool:
        """Check whether the scheduler is running."""
        if self._scheduler is None:
            return False
        # Access the underlying APScheduler via the public API
        return self._scheduler.is_running()

    # ── Response helper ──────────────────────────────────────────────

    @staticmethod
    def _json_response(
        data: dict[str, Any],
        status: int = 200,
    ) -> Any:
        """Create a JSON HTTP response."""
        from aiohttp import web

        return web.Response(
            body=json.dumps(data, default=str),
            content_type="application/json",
            status=status,
        )
