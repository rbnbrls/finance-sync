"""Low-cardinality, secret-safe downstream destination metrics."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

DESTINATION_PROBES = Counter(
    "finance_sync_destination_probes_total",
    "Explicit downstream destination probes by safe outcome",
    ("destination", "status"),
)
DESTINATION_PROBE_DURATION = Histogram(
    "finance_sync_destination_probe_duration_seconds",
    "Duration of explicit downstream destination probes",
    ("destination", "status"),
)
DESTINATION_PROBE_PARITY = Gauge(
    "finance_sync_destination_probe_parity_count",
    "Latest aggregate downstream parity count by safe metric name",
    ("destination", "status", "metric"),
)

_PARITY_METRICS = frozenset(
    {
        "remote_accounts",
        "unmapped_remote_accounts",
        "remote_assets",
        "remote_activities",
        "canonical_activities",
        "stale_remote_activities",
    }
)


def record_destination_probe(
    *,
    destination: str,
    status: str,
    duration_seconds: float,
    parity: dict[str, int] | None = None,
) -> None:
    """Record bounded status/duration/counts without target IDs or payloads."""
    safe_destination = destination or "unknown"
    safe_status = status or "unknown"
    DESTINATION_PROBES.labels(safe_destination, safe_status).inc()
    DESTINATION_PROBE_DURATION.labels(safe_destination, safe_status).observe(
        max(0.0, duration_seconds)
    )
    for metric, value in (parity or {}).items():
        if metric in _PARITY_METRICS:
            DESTINATION_PROBE_PARITY.labels(
                safe_destination, safe_status, metric
            ).set(max(0, value))
