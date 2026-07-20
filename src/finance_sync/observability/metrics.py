"""Prometheus metrics collection and exposition.

Provides pre-defined metric objects for HTTP, database, and business-level
observability, an ASGI middleware that captures per-request metrics, and
a ready-to-mount ASGI app for the ``/metrics`` scrape endpoint.
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,  # type: ignore[reportUnknownVariableType]
)

# ── HTTP metrics (recorded by PrometheusMiddleware) ──────────────────

http_requests_total = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    labelnames=["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "path", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

http_request_size_bytes = Histogram(
    "http_request_size_bytes",
    "HTTP request body size in bytes",
    labelnames=["method", "path"],
)

http_response_size_bytes = Histogram(
    "http_response_size_bytes",
    "HTTP response body size in bytes",
    labelnames=["method", "path"],
)

# ── Database connection pool metrics ────────────────────────────────

db_pool_min = Gauge("db_pool_min", "Minimum database pool size")
db_pool_max = Gauge("db_pool_max", "Maximum database pool size")
db_pool_used = Gauge("db_pool_used", "Currently used database connections")
db_pool_available = Gauge("db_pool_available", "Available database connections")

# ── Business / sync metrics ─────────────────────────────────────────

sync_runs_total = Counter(
    "sync_runs_total",
    "Total number of sync runs by provider and status",
    labelnames=["provider", "status"],
)

transactions_ingested_total = Counter(
    "transactions_ingested_total",
    "Total number of transactions ingested by provider",
    labelnames=["provider"],
)

# Paths to exclude from metrics recording
_SKIP_PATHS = frozenset(
    {"/metrics", "/health", "/health/ready", "/health/live"}
)


class PrometheusMiddleware:
    """ASGI middleware that records per-request Prometheus metrics.

    Captures request count, duration, and body sizes for every HTTP
    request, excluding the static ``/metrics`` and ``/health*`` paths
    to avoid recursion and noise.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # Skip infra-only paths
        if path in _SKIP_PATHS or path.startswith("/health/"):
            await self.app(scope, receive, send)
            return

        # Measure request body size
        request_body_size: int = 0
        original_receive = receive

        async def counting_receive() -> dict[str, Any]:
            nonlocal request_body_size
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                request_body_size += len(body)
            return message

        status_code: list[int | None] = [None]
        response_body_size: int = 0
        start = time.perf_counter()

        async def counting_send(message: dict[str, Any]) -> None:
            nonlocal response_body_size
            if message.get("type") == "http.response.start":
                status_code[0] = message.get("status")
            if message.get("type") == "http.response.body":
                body = message.get("body", b"")
                response_body_size += len(body)
            await send(message)

        await self.app(scope, counting_receive, counting_send)

        duration = time.perf_counter() - start
        status = str(status_code[0] or 0)

        http_requests_total.labels(
            method=method, path=path, status=status
        ).inc()
        http_request_duration_seconds.labels(
            method=method, path=path, status=status
        ).observe(duration)
        http_request_size_bytes.labels(method=method, path=path).observe(
            request_body_size
        )
        http_response_size_bytes.labels(method=method, path=path).observe(
            response_body_size
        )


# ── ASGI app to expose metrics for scraping ─────────────────────────

metrics_app = make_asgi_app()  # type: ignore[reportUnknownVariableType]
