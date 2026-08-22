"""Validate and evaluate the safe SLO alert contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_LABELS = {
    "tenant_id", "account_id", "iban", "credential", "token", "amount"
}
REQUIRED_SLOS = {
    "sync_success_rate",
    "sync_duration_p95",
    "outbox_lag",
    "worker_failure_rate",
}


def validate(config: dict[str, Any]) -> None:
    slos = {str(item.get("name")): item for item in config.get("slos", [])}
    missing = REQUIRED_SLOS - set(slos)
    if missing:
        message = "missing SLOs: " + ", ".join(sorted(missing))
        raise ValueError(message)
    if not config.get("maintenance_suppression"):
        message = "maintenance suppression is required"
        raise ValueError(message)
    for name, slo in slos.items():
        labels = set(slo.get("labels", []))
        forbidden = labels & FORBIDDEN_LABELS
        if forbidden:
            message = f"sensitive metric labels in {name}: {sorted(forbidden)}"
            raise ValueError(message)
        if slo.get("severity") not in {"warning", "critical"}:
            message = f"invalid severity in {name}"
            raise ValueError(message)
        if not slo.get("runbook"):
            message = f"missing runbook in {name}"
            raise ValueError(message)


def evaluate(
    config: dict[str, Any],
    metrics: dict[str, float],
    *,
    maintenance: bool = False,
) -> list[dict[str, str]]:
    validate(config)
    if maintenance:
        return []
    checks = {
        "sync_success_rate": metrics.get("sync_success_rate", 1.0) < 0.99,
        "sync_duration_p95": metrics.get("sync_duration_p95", 0.0) > 900,
        "outbox_lag": metrics.get("outbox_lag", 0.0) > 50,
        "worker_failure_rate": metrics.get("worker_failure_rate", 0.0) > 0.01,
    }
    return [
        {
            "name": str(slo["name"]),
            "severity": str(slo["severity"]),
            "runbook": str(slo["runbook"]),
        }
        for slo in config["slos"]
        if checks[str(slo["name"])]
    ]


def main() -> int:
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("config/slo-alerts.json")
    )
    try:
        validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"SLO alert policy failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
