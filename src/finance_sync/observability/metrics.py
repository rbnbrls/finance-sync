"""Prometheus metrics used by exporter integrations."""

from prometheus_client import Counter, Gauge

export_runs_total = Counter(
    "export_runs_total",
    "Total number of export runs by exporter and status",
    labelnames=["exporter", "status"],
)

outbox_messages_pending_total = Gauge(
    "outbox_messages_pending_total",
    "Number of pending outbox messages awaiting publication",
)
