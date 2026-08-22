"""Compare a PostgreSQL read benchmark report with its stored baseline."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


def compare(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    latency_tolerance: float,
    fail_latency: bool,
) -> dict[str, Any]:
    baseline_operations = baseline.get("operations", {})
    comparisons: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for result in current.get("results", []):
        operation = result["operation"]
        previous = baseline_operations.get(operation)
        if previous is None:
            warnings.append(f"no baseline for {operation}")
            continue
        query_count = int(result["query_count"])
        budget = int(result["budget"])
        latency = float(result["latency_ms"])
        if query_count > budget:
            failures.append(f"{operation}: query budget exceeded")
        baseline_latency = float(previous["latency_ms"])
        ratio = latency / baseline_latency if baseline_latency else 0.0
        if baseline_latency and ratio > 1 + latency_tolerance:
            message = f"{operation}: latency regression {ratio:.2f}x"
            (failures if fail_latency else warnings).append(message)
        comparisons.append(
            {
                "operation": operation,
                "dataset": result.get("dataset"),
                "query_count": query_count,
                "budget": budget,
                "latency_ms": latency,
                "baseline_latency_ms": baseline_latency,
                "latency_ratio": round(ratio, 3),
            }
        )
    return {
        "metadata": {
            **baseline.get("metadata", {}),
            "current_postgres_version": current.get("postgres_version"),
            "python_version": current.get("python_version"),
            "hardware": platform.platform(),
        },
        "comparisons": comparisons,
        "warnings": warnings,
        "failures": failures,
        "latency_tolerance": latency_tolerance,
        "latency_failure_enabled": fail_latency,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--latency-tolerance", type=float, default=0.25)
    parser.add_argument(
        "--fail-latency",
        action="store_true",
        default=os.environ.get("PERF_FAIL_LATENCY") == "true",
    )
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    report = compare(
        baseline,
        current,
        latency_tolerance=args.latency_tolerance,
        fail_latency=args.fail_latency,
    )
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["failures"]:
        message = "read performance gate failed: " + ", ".join(
            report["failures"]
        )
        sys.stderr.write(message + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
