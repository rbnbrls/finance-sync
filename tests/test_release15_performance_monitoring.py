"""Release 15 read-performance monitoring contracts."""

# pyright: basic

import json
from pathlib import Path
from typing import Any

from scripts.check_read_performance import compare

ROOT = Path(__file__).parents[1]


def _baseline() -> dict[str, Any]:
    return {"metadata": {"postgres_version": "PostgreSQL 16"}, "operations": {"portfolio": {"latency_ms": 10}}}


def _current(*, query_count: int = 2, latency_ms: float = 10) -> dict[str, Any]:
    return {
        "postgres_version": "PostgreSQL 16",
        "python_version": "3.12",
        "results": [{"operation": "portfolio", "query_count": query_count, "budget": 2, "latency_ms": latency_ms, "dataset": "holdings-1000"}],
    }


def test_query_budget_is_hard_and_latency_is_configurable() -> None:
    assert compare(_baseline(), _current(), latency_tolerance=0.25, fail_latency=False)["failures"] == []
    result = compare(_baseline(), _current(query_count=3), latency_tolerance=0.25, fail_latency=False)
    assert result["failures"] == ["portfolio: query budget exceeded"]
    warning = compare(_baseline(), _current(latency_ms=13), latency_tolerance=0.25, fail_latency=False)
    assert warning["warnings"]
    failure = compare(_baseline(), _current(latency_ms=13), latency_tolerance=0.25, fail_latency=True)
    assert failure["failures"]


def test_ci_compares_and_uploads_performance_report() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "check_read_performance.py" in workflow
    assert "read-performance-baseline.json" in workflow
    assert "read-performance-comparison.json" in workflow
    json.dumps(_current())
