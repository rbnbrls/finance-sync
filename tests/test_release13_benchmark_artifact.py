"""CI contract tests for the PostgreSQL read benchmark artifact."""

import json
from pathlib import Path

from finance_sync.services.read.benchmarking import (
    ReadBenchmarkResult,
    write_benchmark_report,
)

ROOT = Path(__file__).parents[1]


def test_ci_runs_and_uploads_postgres_benchmark_artifact() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    benchmark = "tests/integration/test_read_query_benchmarks_pg.py"
    assert benchmark in workflow
    assert "READ_BENCHMARK_ARTIFACT: read-benchmarks.json" in workflow
    assert "name: read-query-benchmarks" in workflow
    assert "path: read-benchmarks.json" in workflow


def test_benchmark_artifact_contains_gate_and_diagnostic_fields(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "read-benchmarks.json"
    write_benchmark_report(
        destination,
        results=[
            ReadBenchmarkResult(
                dataset="holdings-100",
                holding_count=100,
                account_count=5,
                operation="portfolio",
                budget=4,
                query_count=3,
                latency_ms=12.5,
            )
        ],
        postgres_version="PostgreSQL 16",
        python_version="3.12.0",
    )
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["postgres_version"] == "PostgreSQL 16"
    result = report["results"][0]
    assert result["dataset"] == "holdings-100"
    assert result["holding_count"] == 100
    assert result["account_count"] == 5
    assert result["query_count"] <= result["budget"]
    assert result["latency_ms"] == 12.5
