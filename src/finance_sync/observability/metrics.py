"""Prometheus metrics collection and exposition.

Provides pre-defined metric objects for HTTP, database, and business-level
observability, an ASGI middleware that captures per-request metrics, and
a ready-to-mount ASGI app for the ``/metrics`` scrape endpoint.
"""

from __future__ import annotations

import time
from typing import Any, cast

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

holdings_ingested_total = Counter(
    "holdings_ingested_total",
    "Total number of holding snapshots ingested by provider",
    labelnames=["provider"],
)

unresolved_securities_total = Counter(
    "unresolved_securities_total",
    "Total number of unresolved security references by provider",
    labelnames=["provider"],
)

sync_run_duration_seconds = Gauge(
    "sync_run_duration_seconds",
    "Duration of the most recent sync run in seconds",
    labelnames=["provider"],
)

# ── Outbox / pipeline health ────────────────────────────────────────

outbox_messages_pending_total = Gauge(
    "outbox_messages_pending_total",
    "Number of pending outbox messages awaiting publication",
)

enrichment_last_success_timestamp = Gauge(
    "enrichment_last_success_timestamp",
    "Unix timestamp of the last successful enrichment run",
)

export_runs_total = Counter(
    "export_runs_total",
    "Total number of export runs by exporter and status",
    labelnames=["exporter", "status"],
)

# ── Worker job metrics (exposed on the worker's /metrics) ──────────

worker_job_duration_seconds = Gauge(
    "worker_job_duration_seconds",
    "Duration of the most recent run for a scheduled worker job",
    labelnames=["job_id"],
)

worker_job_success_rate = Gauge(
    "worker_job_success_rate",
    "Success rate (0-1) of a scheduled worker job",
    labelnames=["job_id"],
)

# ── Data-quality remediation ───────────────────────────────────────

data_quality_remediation_backlog_size = Gauge(
    "data_quality_remediation_backlog_size",
    "Number of remediation items by lifecycle status",
    labelnames=["status"],
)
data_quality_remediation_pending_by_provider = Gauge(
    "data_quality_remediation_pending_by_provider",
    "Due and retryable remediation items by provider",
    labelnames=["provider"],
)
data_quality_remediation_throughput_total = Counter(
    "data_quality_remediation_throughput_total",
    "Remediation outcomes by provider and strategy",
    labelnames=["provider", "strategy", "outcome"],
)
data_quality_remediation_attempts_total = Counter(
    "data_quality_remediation_attempts_total",
    "Provider execution attempts by provider and strategy",
    labelnames=["provider", "strategy"],
)
data_quality_remediation_deferred_rate_limit_total = Counter(
    "data_quality_remediation_deferred_rate_limit_total",
    "Items deferred because provider quota was unavailable",
    labelnames=["provider", "endpoint_family"],
)
data_quality_remediation_batches_created_total = Counter(
    "data_quality_remediation_batches_created_total",
    "Remediation batches created",
)
data_quality_remediation_batch_items_total = Counter(
    "data_quality_remediation_batch_items_total",
    "Items included in remediation batches",
)
data_quality_remediation_average_items_per_batch = Gauge(
    "data_quality_remediation_average_items_per_batch",
    "Average number of items in the most recent remediation batches",
)
data_quality_remediation_api_calls_saved_total = Counter(
    "data_quality_remediation_api_calls_saved_total",
    "Provider calls avoided through remediation batching",
)
data_quality_remediation_failures_total = Counter(
    "data_quality_remediation_failures_total",
    "Terminal and retryable remediation failures",
    labelnames=["provider", "strategy", "category"],
)
data_quality_remediation_oldest_age_seconds = Gauge(
    "data_quality_remediation_oldest_age_seconds",
    "Age of the oldest due remediation item",
)
data_quality_remediation_manual_review_size = Gauge(
    "data_quality_remediation_manual_review_size",
    "Number of remediation items awaiting manual review",
)

wealthfolio_health_polls_total = Counter(
    "wealthfolio_health_polls_total",
    "Wealthfolio health polls by outcome",
    labelnames=["outcome"],
)
wealthfolio_health_imported_issues_total = Counter(
    "wealthfolio_health_imported_issues_total",
    "Wealthfolio health issues imported into remediation",
)
wealthfolio_health_repairs_total = Counter(
    "wealthfolio_health_repairs_total",
    "Wealthfolio health repair outcomes",
    labelnames=["outcome"],
)
wealthfolio_health_poll_duration_seconds = Histogram(
    "wealthfolio_health_poll_duration_seconds",
    "Duration of Wealthfolio health bridge polls",
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

metrics_app: Any = cast("Any", make_asgi_app())
