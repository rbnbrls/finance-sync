"""Utilities for deterministic PostgreSQL read benchmark reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ReadBenchmarkResult:
    """One measured read operation in a benchmark dataset."""

    dataset: str
    holding_count: int
    account_count: int
    operation: str
    budget: int
    query_count: int
    latency_ms: float


def write_benchmark_report(
    path: str | Path,
    *,
    results: list[ReadBenchmarkResult],
    postgres_version: str,
    python_version: str,
) -> None:
    """Write a stable, machine-readable report for CI artifacts."""
    report: dict[str, Any] = {
        "postgres_version": postgres_version,
        "python_version": python_version,
        "results": [asdict(result) for result in results],
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
