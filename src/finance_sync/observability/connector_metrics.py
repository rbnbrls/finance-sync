"""Low-cardinality, secret-safe connector lifecycle metrics."""

from __future__ import annotations

import hashlib

from prometheus_client import Counter, Histogram

CONNECTOR_OPERATIONS = Counter(
    "finance_sync_connector_operations_total",
    "Connector operations by safe outcome",
    (
        "provider",
        "connector_version",
        "connection_hash",
        "resource",
        "status",
        "error_category",
    ),
)
CONNECTOR_OPERATION_DURATION = Histogram(
    "finance_sync_connector_operation_duration_seconds",
    "Connector operation duration",
    ("provider", "connector_version", "resource", "status"),
)
CONNECTOR_RETRIES = Counter(
    "finance_sync_connector_retries_total",
    "Connector retry attempts",
    ("provider", "resource"),
)
CONNECTOR_RATE_LIMITS = Counter(
    "finance_sync_connector_rate_limits_total",
    "Connector rate-limit diagnoses",
    ("provider", "scope"),
)


def connection_hash(connection_id: str | None) -> str:
    """Return a stable non-reversible identifier for external telemetry."""
    if connection_id is None:
        return "none"
    # After the None check, connection_id must be str (per type annotation)
    return hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:16]


def record_connector_operation(
    *,
    provider: str,
    connector_version: str | None,
    connection_id: str | None,
    resource: str,
    status: str,
    duration_seconds: float,
    error_category: str | None = None,
    retries: int = 0,
    rate_limit_count: int = 0,
    rate_limit_scope: str | None = None,
) -> None:
    """Record safe operational dimensions; never accepts payload/error text."""
    version = connector_version or "unknown"
    category = error_category or "none"
    CONNECTOR_OPERATIONS.labels(
        provider,
        version,
        connection_hash(connection_id),
        resource,
        status,
        category,
    ).inc()
    CONNECTOR_OPERATION_DURATION.labels(
        provider, version, resource, status
    ).observe(max(0.0, duration_seconds))
    if retries:
        CONNECTOR_RETRIES.labels(provider, resource).inc(retries)
    if rate_limit_count:
        scope = rate_limit_scope or "unknown"
        CONNECTOR_RATE_LIMITS.labels(provider, scope).inc(rate_limit_count)
