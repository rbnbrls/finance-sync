"""Release 16 SLO and alerting contracts."""

# pyright: basic

import json
from pathlib import Path

import pytest

from scripts.check_slo_alerts import evaluate, validate


def _config() -> dict:
    return json.loads(Path("config/slo-alerts.json").read_text())


def test_slos_have_safe_labels_severity_runbooks_and_suppression() -> None:
    config = _config()
    validate(config)
    assert config["owner"] == "finance-platform-oncall"
    assert config["maintenance_suppression"] == "maintenance-window"
    assert all("tenant_id" not in item["labels"] for item in config["slos"])


def test_synthetic_failures_trigger_alerts_and_maintenance_suppresses() -> None:
    config = _config()
    failures = evaluate(
        config,
        {
            "sync_success_rate": 0.9,
            "sync_duration_p95": 1200,
            "outbox_lag": 80,
            "worker_failure_rate": 0.2,
        },
    )
    assert {item["name"] for item in failures} == {
        "sync_success_rate",
        "sync_duration_p95",
        "outbox_lag",
        "worker_failure_rate",
    }
    assert all(item["severity"] in {"warning", "critical"} for item in failures)
    assert evaluate(config, {"outbox_lag": 100}, maintenance=True) == []


def test_invalid_sensitive_label_fails_policy() -> None:
    config = _config()
    config["slos"][0]["labels"].append("tenant_id")
    with pytest.raises(ValueError, match="sensitive metric labels"):
        validate(config)


def test_ci_validates_slo_alert_policy() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "check_slo_alerts.py" in workflow
    assert "slo-alerts.json" in workflow
