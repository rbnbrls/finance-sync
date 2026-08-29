"""Deterministic release 19 autoscaling load-test evidence."""

import json
from pathlib import Path

import pytest

from scripts.loadtest_autoscaling import build_report, run_profile

CONFIG = Path("config/loadtest-profiles.json")


def test_config_defines_all_required_load_profiles() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    names = {profile["name"] for profile in config["profiles"]}
    assert names == {"api_reads", "sync_runs", "retries", "outbox_consumers"}
    assert config["synthetic_data_only"] is True


def test_profile_reports_operational_metrics_without_financial_data() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    result = run_profile(config, config["profiles"][0])
    assert {
        "latency_p95_ms",
        "error_rate",
        "queue_depth_max",
        "database_connections_max",
        "worker_count_max",
    } <= result.keys()
    assert result["financial_values_in_report"] is False
    assert result["provider_rate_limit_respected"] is True


def test_overload_is_rejected_without_duplicate_writes() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    overload = next(p for p in config["profiles"] if p["name"] == "sync_runs")
    result = run_profile(config, {**overload, "requests": 10_000})
    assert result["overload_action"] == "reject_with_retry_after"
    assert result["duplicate_writes"] == 0
    assert result["backpressure_respected"] is True


def test_report_is_reproducible_and_contains_scaling_advice() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert build_report(config) == build_report(config)
    report = build_report(config)
    assert len(report["profiles"]) == 4
    assert report["scaling_recommendation"]
    assert report["financial_values_in_report"] is False


def test_invalid_profile_is_rejected() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="profile requests must be positive"):
        run_profile(config, {"name": "bad", "requests": 0})
