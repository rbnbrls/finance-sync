"""Publish a safe, machine-readable release gate and sync-health summary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_GATES = (
    "unit",
    "integration",
    "e2e",
    "migration",
    "security",
    "benchmark",
    "staging",
)


def build_summary(
    artifacts: dict[str, Path],
    *,
    now: float,
    max_age_hours: float,
    sync_status: str,
    outbox_lag: str,
) -> dict[str, Any]:
    failures: list[str] = []
    gates: list[dict[str, Any]] = []
    for gate in REQUIRED_GATES:
        path = artifacts.get(gate)
        status = "passed"
        reason = None
        if path is None:
            status, reason = "failed", "artifact not configured"
        elif not path.is_file() or path.stat().st_size == 0:
            status, reason = "failed", "artifact missing or empty"
        elif now - path.stat().st_mtime > max_age_hours * 3600:
            status, reason = "failed", "artifact too old"
        if reason:
            failures.append(f"{gate}: {reason}")
        entry: dict[str, Any] = {"name": gate, "status": status}
        if path is not None:
            entry["artifact"] = str(path)
        if reason:
            entry["reason"] = reason
        gates.append(entry)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "gates": gates,
        "sync_health": {"status": sync_status, "outbox_lag": outbox_lag},
        "contains_financial_data": False,
        "contains_secrets": False,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact", action="append", default=[], metavar="GATE=PATH"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument(
        "--sync-status", default=os.environ.get("SYNC_HEALTH_STATUS", "unknown")
    )
    parser.add_argument(
        "--outbox-lag", default=os.environ.get("OUTBOX_LAG", "unknown")
    )
    args = parser.parse_args()
    artifacts: dict[str, Path] = {}
    for item in args.artifact:
        gate, separator, path = item.partition("=")
        if not separator or not gate or not path:
            parser.error(f"invalid artifact mapping: {item!r}")
        artifacts[gate] = Path(path)
    report = build_summary(
        artifacts,
        now=__import__("time").time(),
        max_age_hours=args.max_age_hours,
        sync_status=args.sync_status,
        outbox_lag=args.outbox_lag,
    )
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if report["failures"]:
        sys.stderr.write(
            "operational gate failed: " + "; ".join(report["failures"]) + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
