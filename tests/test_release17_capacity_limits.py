"""Release 17 capacity-limit contracts."""

# pyright: basic

import json
from pathlib import Path

from scripts.capacity_limit_report import DATASETS, build_report


def test_capacity_report_covers_deterministic_datasets_and_metrics() -> None:
    config = json.loads(Path("config/capacity-limits.json").read_text())
    report = build_report(config)
    assert [item["holdings"] for item in report["datasets"]] == list(DATASETS)
    for result in report["datasets"]:
        assert result["query_count"] == 3
        assert result["transactions"] == result["holdings"] * 12
        assert result["concurrent_workers"] == 2
        assert result["rate_limited_connector"] is True
        assert result["synthetic_data_only"] is True
    assert report["financial_values_in_report"] is False


def test_capacity_limits_and_deployment_recommendation_are_explicit() -> None:
    config = json.loads(Path("config/capacity-limits.json").read_text())
    assert config["soft_limits"]["holdings"] < config["hard_limits"]["holdings"]
    assert config["recommended_deployment"]["sync_workers"] >= 2


def test_ci_publishes_capacity_artifact() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "capacity-limits:" in workflow
    assert "capacity_limit_report.py" in workflow
    assert "capacity-limits.json" in workflow
